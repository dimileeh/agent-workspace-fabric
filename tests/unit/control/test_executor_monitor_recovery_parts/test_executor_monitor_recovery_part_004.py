"""Executor recovery branch — additional recovery scenario tests.

Split from test_executor_monitor_recovery_part_001.py — normal-path
regression guard and existing-PR recovery scenarios.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters import registry as _registry  # noqa: F401 — populates registry
from awf.common.commands import FakeCommandRunner
from awf.control.executor import (
    ExecutorConfig,
    WorkspaceExecutor,
)
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.repositories import (
    OperationRepository,
    WorkspaceEventRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.node.compose_manager import ComposeManager
from awf.runtime.pr_creator import PullRequestCreator
from awf.runtime.validation import ValidationRunner
from tests.postgres import postgres_test_engine
from tests.unit.control.executor_paths import _test_worktrees_root

_TEMPLATE = Path(__file__).resolve().parents[3] / "docker" / "compose" / "workspace.base.yml.j2"


def _queue_validation_head(fake: FakeCommandRunner, head: str = "deadbeef01") -> None:
    fake.queue_result(returncode=0, stdout=f"{head}\n")


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        session_factory = make_session_factory(engine)
        session_factory._awf_test_worktrees_root = tmp_path / "work" / "worktrees"  # type: ignore[attr-defined]
        yield session_factory


@pytest.fixture
def fake() -> FakeCommandRunner:
    return FakeCommandRunner()


_FEATURE_TASK_PROMPT = "Implement the customer feature flag wiring for the staging dashboard."


def _make_executor(
    *,
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    max_fix_passes: int = 5,
    pr_monitor_factory: Any = None,
    validation: Any = None,
) -> WorkspaceExecutor:
    compose = ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE)
    validation = validation or ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts")
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
        pr_monitor_factory=pr_monitor_factory,
    )


async def _seed_ready_workspace_with_recovery(
    factory: async_sessionmaker[AsyncSession],
    *,
    pr_url: str = "https://github.com/x/y/pull/1",
    pr_number: int = 1,
    create_worktree: bool = True,
    resolved_profile: dict[str, Any] | None = None,
) -> str:
    from awf.db.enums import OperationType

    async with factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.create(
            repo_url="git@github.com:dimileeh/aira-agent.git",
            branch_base="development",
            task_title="recovery test",
            task_prompt=_FEATURE_TASK_PROMPT,
            agent="codex",
            test_commands=["pytest -q"],
            requires_database=False,
            resolved_profile=resolved_profile,
        )
        await repo.transition(ws, to=WorkspaceStatus.provisioning, reason_code="X")
        ws.branch_name = f"awf/{ws.id}"
        ws.base_commit = "a" * 40
        ws.monitor_last_commit_sha = "d" * 40
        ws.compose_project_name = f"awf_{ws.id}"
        ws.pr_url = pr_url
        ws.pr_number = pr_number
        ws.remote_push_branch = ws.branch_name
        await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="X")
        await repo.transition(ws, to=WorkspaceStatus.running, reason_code="X")
        await repo.transition(ws, to=WorkspaceStatus.validating, reason_code="X")
        await repo.transition(ws, to=WorkspaceStatus.pushing, reason_code="X")
        await repo.transition(ws, to=WorkspaceStatus.monitoring_pr, reason_code="X")
        await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="RECOVERY_DISPATCH")
        payload = {
            "owner": "pr_monitor",
            "source": "pr_monitor",
            "action": "validate_only",
            "requested_action": "validate",
            "reason": "validation_insufficient_tier",
            "reason_code": "VALIDATION_INSUFFICIENT_TIER",
            "recovery_mode": "validate_only",
            "pr_number": pr_number,
            "pr_url": pr_url,
            "source_head_sha": ws.monitor_last_commit_sha,
            "source_base_sha": ws.base_commit,
            "target_branch": ws.branch_base,
            "remote_branch": ws.remote_push_branch,
        }
        await OperationRepository(s).create(
            workspace_id=ws.id,
            operation_type=OperationType.validate,
            payload=payload,
            idempotency_key=f"pr_monitor:validate_only:{ws.id}",
        )
        await s.commit()
        if create_worktree:
            (_test_worktrees_root(factory) / ws.id).mkdir(parents=True, exist_ok=True)
        return ws.id


async def _seed_ready_workspace_no_recovery(
    factory: async_sessionmaker[AsyncSession],
    *,
    create_worktree: bool = True,
) -> str:
    async with factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.create(
            repo_url="git@github.com:dimileeh/aira-agent.git",
            branch_base="development",
            task_title="normal feature",
            task_prompt=_FEATURE_TASK_PROMPT,
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
        if create_worktree:
            (_test_worktrees_root(factory) / ws.id).mkdir(parents=True, exist_ok=True)
        return ws.id


def _queue_push_and_pr(
    fake: FakeCommandRunner, *, pr_url: str = "https://github.com/x/y/pull/1"
) -> None:
    fake.queue_result(returncode=0, stdout="M\0src/fix.py\0")
    fake.queue_result(returncode=0, stdout="deadbeef01\n")
    fake.queue_result(returncode=0, stdout="awf/ws_test\n")
    fake.queue_result(returncode=0, stdout="abc1234 work\n")
    fake.queue_result(returncode=0)
    fake.queue_result(returncode=0, stdout=pr_url)


def _all_adapter_args(fake: FakeCommandRunner) -> list[list[str]]:
    return [c.args for c in fake.calls if "exec" in c.args and "codex" in c.args]


def _all_adapter_prompt_values(fake: FakeCommandRunner) -> list[str]:
    prompts: list[str] = []
    for call in fake.calls:
        if "exec" not in call.args or "codex" not in call.args:
            continue
        if call.input_bytes is not None:
            prompts.append(call.input_bytes.decode())
    return prompts


def _all_adapter_prompts(fake: FakeCommandRunner) -> str:
    return "\n".join(_all_adapter_prompt_values(fake))


def _all_push_and_pr_create_calls(fake: FakeCommandRunner) -> list[list[str]]:
    return [
        c.args
        for c in fake.calls
        if ("push" in c.args and "git" in c.args) or (c.args[:3] == ["gh", "pr", "create"])
    ]


@pytest.mark.unit
async def test_executor_normal_path_unchanged_when_no_recovery_op(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Regression guard: workspaces without a validate-only recovery op
    must continue running planning/agent/feature execution as before.
    """
    executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path)
    ws_id = await _seed_ready_workspace_no_recovery(factory)

    fake.queue_result(returncode=0, stdout="codex finished")
    fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")
    fake.queue_result(returncode=0)
    fake.queue_result(returncode=0, stdout="CHANGELOG.md\n")
    fake.queue_result(returncode=0)
    fake.queue_result(returncode=0, stdout="1\n")
    fake.queue_result(returncode=0)
    _queue_validation_head(fake)
    fake.queue_result(returncode=0, stdout="tests ok")
    _queue_push_and_pr(fake)

    await executor.execute(ws_id)

    adapter_invocations = _all_adapter_args(fake)
    assert len(adapter_invocations) == 1
    prompts = _all_adapter_prompts(fake)
    assert _FEATURE_TASK_PROMPT in prompts

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.completed.value


@pytest.mark.unit
async def test_recovery_skips_push_when_pr_already_exists(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A workspace in recovery with an existing PR must NOT re-push or
    re-create the PR. The executor should skip the entire push/PR-creation
    path and transition directly back to monitoring_pr (or completed if
    no monitor is wired)."""
    executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path)
    ws_id = await _seed_ready_workspace_with_recovery(
        factory, pr_url="https://github.com/x/y/pull/1"
    )

    _queue_validation_head(fake, head="d" * 40)
    fake.queue_result(returncode=0, stdout="tests ok")

    await executor.execute(ws_id)

    assert _all_push_and_pr_create_calls(fake) == []

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        events = await WorkspaceEventRepository(s).list(workspace_id=ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.completed.value
        assert any(
            event.event_type == "workspace.state_changed"
            and event.reason_code == "RECOVERY_VALIDATION_OK"
            and event.old_state == WorkspaceStatus.validating.value
            and event.new_state == WorkspaceStatus.completed.value
            for event in events
        )
