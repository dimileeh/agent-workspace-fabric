"""Regression: post-agent git capture holds the worktree writer lock."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters import registry as _registry  # noqa: F401 — populate registry
from awf.common.commands import FakeCommandRunner
from awf.control.executor import (
    ExecutorConfig,
    WorkspaceExecutor,
    quality_methods,
    quality_methods_post_agent,
)
from awf.control.executor import execution_flow as execution_flow_mod
from awf.control.executor.quality_gates import _PostAgentCommitClassification
from awf.db.enums import AgentRuntime
from awf.db.session import make_session_factory
from awf.profiles.models import WorkspaceProfile
from awf.runtime.pr_creator import PullRequestCreator
from awf.runtime.worktree_writer_lock import hold_exclusive_worktree_writer_lock
from tests.postgres import postgres_test_engine
from tests.unit.control.test_executor_error_paths_parts.test_executor_error_paths_part_001 import (
    _NoopResumeCompose,
    _RecordingValidation,
    _seed_ready,
)


def _semantic_classification() -> _PostAgentCommitClassification:
    return _PostAgentCommitClassification(
        reason_code="POST_AGENT_COMMIT_PRECOMMIT_FAILED",
        failed_hooks=("awf-ruff-check",),
        format_repair_files=(),
        normalizer_repair_files=(),
        autofix_repair_files=(),
        summary="semantic pre-commit failure",
        repair_strategy="agent",
    )


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
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> WorkspaceExecutor:
    return WorkspaceExecutor(
        session_factory=factory,
        runner=fake,
        compose=_NoopResumeCompose(),
        validation=_RecordingValidation(),
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
    )


@pytest.mark.unit
async def test_post_agent_git_capture_holds_exclusive_worktree_writer_lock(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws_id = await _seed_ready(factory)
    fake.queue_result(returncode=0, stdout="adapter ok")
    fake.queue_result(returncode=0, stdout="awf/x\n")
    fake.queue_result(returncode=0)
    fake.queue_result(returncode=0, stdout="src/awf/foo.py\n")
    fake.queue_result(returncode=0)
    fake.queue_result(returncode=0, stdout="0\n")

    lock_entered = False
    original_lock = hold_exclusive_worktree_writer_lock

    @contextlib.asynccontextmanager
    async def _spy_writer_lock(worktree_path: Path):
        nonlocal lock_entered
        lock_entered = True
        async with original_lock(worktree_path):
            yield

    monkeypatch.setattr(
        execution_flow_mod,
        "hold_exclusive_worktree_writer_lock",
        _spy_writer_lock,
    )

    executor = _make_executor(fake, factory, tmp_path)
    await executor.execute(ws_id)

    assert lock_entered is True


@pytest.mark.unit
async def test_post_agent_semantic_precommit_repair_skips_nested_writer_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression PRRT_kwDOSJAM6s6bh2OY: repair must not re-acquire the outer lock."""
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()
    recovery_kwargs: dict[str, Any] = {}

    async def _record_service_recovery(self: Any, **kwargs: Any) -> tuple[bool, Any]:
        del self
        recovery_kwargs.update(kwargs)
        return True, await kwargs["run_agent"](False)

    monkeypatch.setattr(
        quality_methods_post_agent,
        "_run_agent_callable_with_service_recovery",
        _record_service_recovery,
    )

    self_obj = SimpleNamespace(
        _record_post_agent_commit_format_repair=AsyncMock(),
        _repair_agent_git_ownership=AsyncMock(),
        _refresh_supply_chain_policy_for_workspace=AsyncMock(
            return_value=SimpleNamespace(policy_blocked=False, findings=())
        ),
        _committed_and_staged_output_is_plan_only=AsyncMock(return_value=False),
        _fail_if_plan_only_paths=AsyncMock(return_value=False),
        _protected_file_diffs_for_staged_paths=AsyncMock(return_value={}),
        _active_operator_grant_specs=AsyncMock(return_value=[]),
    )

    async def _git_in_worktree(args: list[str]) -> Any:
        from awf.common.commands import CommandResult

        if args[:1] == ["add"]:
            return CommandResult(returncode=0, stdout="", stderr="")
        if args[:3] == ["diff", "--cached", "--name-only"]:
            return CommandResult(returncode=0, stdout="src/app.py\n", stderr="")
        return CommandResult(returncode=0, stdout="", stderr="")

    adapter = SimpleNamespace(
        is_hosted=False,
        run=AsyncMock(return_value=SimpleNamespace(stdout="", stderr="")),
    )

    async with hold_exclusive_worktree_writer_lock(worktree_path):
        result = await quality_methods._run_post_agent_semantic_precommit_repair(  # noqa: SLF001
            self_obj,
            workspace_id="ws_nested_writer_lock",
            worktree_path=worktree_path,
            base_commit="a" * 40,
            commit_result=SimpleNamespace(),
            classification=_semantic_classification(),
            staged_paths=["src/app.py"],
            run_commit=AsyncMock(return_value=SimpleNamespace(ok=True)),
            git_in_worktree=_git_in_worktree,
            adapter=adapter,
            compose_project="awf_ws_nested_writer_lock",
            compose_file=tmp_path / "compose.yml",
            model=None,
            ws=SimpleNamespace(owned_paths=[]),
            profile=WorkspaceProfile(name="test"),
            command_evidence=[],
            hosted_pr_identity=None,
        )

    assert result is True
    assert recovery_kwargs.get("hold_writer_lock") is False
