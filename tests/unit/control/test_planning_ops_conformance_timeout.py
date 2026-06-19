"""Focused planning-ops conformance timeout coverage."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from awf.adapters.base import AgentRunError
from awf.common.commands import CommandResult, FakeCommandRunner
from awf.control.executor import ExecutorConfig, WorkspaceExecutor
from awf.db.enums import AgentRuntime
from awf.profiles.models import WorkspaceProfile


def _executor_with_runner(runner: FakeCommandRunner, tmp_path: Path) -> WorkspaceExecutor:
    executor = WorkspaceExecutor(
        session_factory=object(),  # type: ignore[arg-type]
        runner=runner,
        compose=object(),  # type: ignore[arg-type]
        validation=object(),  # type: ignore[arg-type]
        pr_creator=object(),  # type: ignore[arg-type]
        config=ExecutorConfig(
            worktrees_root=tmp_path / "worktrees",
            compose_projects_root=tmp_path / "compose",
        ),
    )
    executor._update_subphase = AsyncMock()  # type: ignore[method-assign]
    return executor


class _StdoutTimeoutAdapter:
    """Plan and execute succeed; conformance idles out with stdout JSON only."""

    def __init__(self, *, satisfied_stdout: str) -> None:
        self._satisfied_stdout = satisfied_stdout
        self.prompts: list[str] = []

    async def run(self, **kwargs: object) -> SimpleNamespace:
        prompt = kwargs.get("prompt")
        assert isinstance(prompt, str)
        self.prompts.append(prompt)
        if len(self.prompts) == 3:
            raise AgentRunError(
                agent=AgentRuntime.codex,
                result=CommandResult(
                    returncode=124,
                    stdout=self._satisfied_stdout,
                    stderr="idle timeout",
                ),
                reason_code="AGENT_IDLE_TIMEOUT",
            )
        if len(self.prompts) == 1:
            return SimpleNamespace(stdout="plan written", stderr="")
        return SimpleNamespace(stdout="implementation", stderr="")


@pytest.mark.unit
async def test_planning_conformance_timeout_falls_back_to_stdout_report(
    tmp_path: Path,
) -> None:
    """When no fresh report exists, satisfied timeout stdout completes conformance."""
    worktree = tmp_path / "worktree"
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout="")  # before_plan changed paths
    runner.queue_result(returncode=0, stdout="sha1\n")  # baseline HEAD
    runner.queue_result(returncode=0, stdout="?? docs/awf-plans/ws_stdout.md\n")
    runner.queue_result(returncode=0, stdout="")  # committed paths
    runner.queue_result(returncode=0, stdout="sha1\n")  # implementation baseline HEAD
    runner.queue_result(returncode=0, stdout="?? docs/awf-plans/ws_stdout.md\n")
    runner.queue_result(returncode=0, stdout="?? docs/awf-plans/ws_stdout.md\n")
    runner.queue_result(returncode=0, stdout="sha2\n")  # after_head
    executor = _executor_with_runner(runner, tmp_path)

    profile = WorkspaceProfile.model_validate(
        {
            "name": "planning-stdout",
            "planning": {
                "required": True,
                "plan_path": "docs/awf-plans/{workspace_id}.md",
                "conformance_report_path": "docs/awf-plans/{workspace_id}.json",
                "max_iterations": 0,
            },
        }
    )
    satisfied = '{"status":"satisfied","summary":"validated evidence satisfies plan","gaps":[]}'

    result = await executor._run_agent_task_with_optional_planning(  # noqa: SLF001
        adapter=_StdoutTimeoutAdapter(satisfied_stdout=satisfied),  # type: ignore[arg-type]
        workspace=SimpleNamespace(id="ws_stdout", task_prompt="do it", task_tag=None),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=worktree,
        model=None,
    )

    assert result is None
