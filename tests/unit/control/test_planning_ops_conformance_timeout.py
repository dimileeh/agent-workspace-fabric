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
        self.worktree_paths: list[object] = []
        self.hosted_pr_identities: list[object] = []

    async def run(self, **kwargs: object) -> SimpleNamespace:
        prompt = kwargs.get("prompt")
        assert isinstance(prompt, str)
        self.prompts.append(prompt)
        self.worktree_paths.append(kwargs.get("worktree_path"))
        self.hosted_pr_identities.append(kwargs.get("hosted_pr_identity"))
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


class _CaptureWorktreeAdapter:
    """Capture checkout context forwarded on every adapter.run call."""

    def __init__(self) -> None:
        self.worktree_paths: list[object] = []
        self.hosted_pr_identities: list[object] = []

    async def run(self, **kwargs: object) -> SimpleNamespace:
        self.worktree_paths.append(kwargs.get("worktree_path"))
        self.hosted_pr_identities.append(kwargs.get("hosted_pr_identity"))
        return SimpleNamespace(stdout="ok", stderr="")


@pytest.mark.unit
@pytest.mark.parametrize(
    "stale_status,current_stdout",
    [
        (None, True),
        ("needs_human", False),
        ("needs_human", True),
        ("needs_iteration", True),
        ("satisfied", True),
    ],
)
@pytest.mark.parametrize("failed_call", [False, True])
async def test_initial_conformance_decision_requires_current_report(
    tmp_path: Path,
    stale_status: str | None,
    current_stdout: bool,
    failed_call: bool,
) -> None:
    from tests.unit.runtime.test_conformance_decision import decision_payload

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    if stale_status is not None:
        (worktree / "ws_decision.json").write_text(decision_payload(status=stale_status, gaps=[]))
    runner = FakeCommandRunner()
    for output in (
        "",
        "sha1\n",
        "?? ws_decision.md\n",
        "",
        "sha1\n",
        "?? ws_decision.md\n",
        "?? ws_decision.md\n",
        "sha2\n",
    ):
        runner.queue_result(returncode=0, stdout=output)
    executor = _executor_with_runner(runner, tmp_path)

    class Adapter:
        calls = 0

        async def run(self, **kwargs: object) -> SimpleNamespace:
            self.calls += 1
            if self.calls == 3 and failed_call:
                raise AgentRunError(
                    agent=AgentRuntime.codex,
                    result=CommandResult(
                        returncode=124, stdout=decision_payload(), stderr="timeout"
                    ),
                    reason_code="AGENT_IDLE_TIMEOUT",
                )
            return SimpleNamespace(
                returncode=0,
                stderr="",
                stdout=decision_payload() if self.calls == 3 and current_stdout else "",
            )

    adapter = Adapter()
    profile = WorkspaceProfile.model_validate(
        {
            "name": "decision",
            "planning": {
                "required": True,
                "max_iterations": 0,
                "plan_path": "{workspace_id}.md",
                "conformance_report_path": "{workspace_id}.json",
            },
        }
    )
    result = await executor._run_agent_task_with_optional_planning(  # noqa: SLF001
        adapter=adapter,
        workspace=SimpleNamespace(id="ws_decision", task_prompt="task", task_tag=None),
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=worktree,
        model=None,
    )
    assert type(result).__name__ == (
        "_PlanningRunFailure" if not current_stdout or failed_call else "_PlanningNeedsHuman"
    )
    assert adapter.calls == 3


@pytest.mark.unit
@pytest.mark.parametrize(
    "file_status,stdout_status",
    [("satisfied", "needs_human"), ("needs_human", "satisfied")],
)
async def test_initial_conformance_fresh_file_takes_precedence_over_stdout(
    tmp_path: Path, file_status: str, stdout_status: str
) -> None:
    from awf.control.executor.types import _PlanningNeedsHuman
    from tests.unit.runtime.test_conformance_decision import decision_payload

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    runner = FakeCommandRunner()
    for output in (
        "",
        "sha1\n",
        "?? ws_decision.md\n",
        "",
        "sha1\n",
        "?? ws_decision.md\n",
        "?? ws_decision.md\n?? ws_decision.json\n",
        "sha2\n",
    ):
        runner.queue_result(returncode=0, stdout=output)
    executor = _executor_with_runner(runner, tmp_path)

    class Adapter:
        calls = 0

        async def run(self, **kwargs: object) -> SimpleNamespace:
            self.calls += 1
            stdout = ""
            if self.calls == 3:
                (worktree / "ws_decision.json").write_text(
                    decision_payload(status=file_status, summary="Fresh file report", gaps=[])
                )
                stdout = decision_payload(status=stdout_status, summary="Stdout report", gaps=[])
            return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    adapter = Adapter()
    result = await executor._run_agent_task_with_optional_planning(  # noqa: SLF001
        adapter=adapter,
        workspace=SimpleNamespace(id="ws_decision", task_prompt="task", task_tag=None),
        profile=WorkspaceProfile.model_validate(
            {
                "name": "decision",
                "planning": {
                    "required": True,
                    "max_iterations": 0,
                    "plan_path": "{workspace_id}.md",
                    "conformance_report_path": "{workspace_id}.json",
                },
            }
        ),
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=worktree,
        model=None,
    )
    if file_status == "satisfied":
        assert result is None
    else:
        assert isinstance(result, _PlanningNeedsHuman)
        assert result.report.summary == "Fresh file report"
    assert adapter.calls == 3


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
    hosted_pr_identity = {
        "repo_url": "https://github.com/example/repo.git",
        "pr_number": 778,
        "head_ref": "feature/hosted",
        "expected_head_sha": "a" * 40,
    }

    adapter = _StdoutTimeoutAdapter(satisfied_stdout=satisfied)
    result = await executor._run_agent_task_with_optional_planning(  # noqa: SLF001
        adapter=adapter,  # type: ignore[arg-type]
        workspace=SimpleNamespace(id="ws_stdout", task_prompt="do it", task_tag=None),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=worktree,
        model=None,
        hosted_pr_identity=hosted_pr_identity,
    )

    assert result is None
    # Every agent-facing prompt (plan, execute, conformance) anchors the plan
    # artifact at the worktree root so it cannot land nested under a subdir CWD
    # (#620); the conformance prompt additionally anchors the report path.
    assert len(adapter.prompts) == 3
    assert all("/workspace/docs/awf-plans/ws_stdout.md" in prompt for prompt in adapter.prompts)
    assert "/workspace/docs/awf-plans/ws_stdout.json" in adapter.prompts[2]
    # Hosted agent-start env_file scanning needs the checkout path on every
    # create-path run (plan / execute / conformance), not only later fix paths.
    assert adapter.worktree_paths == [worktree, worktree, worktree]
    assert adapter.hosted_pr_identities == [hosted_pr_identity] * 3


@pytest.mark.unit
async def test_optional_planning_forwards_worktree_path_when_planning_not_required(
    tmp_path: Path,
) -> None:
    """No-planning create-path runs must forward worktree_path to adapter.run."""
    worktree = tmp_path / "worktree"
    executor = _executor_with_runner(FakeCommandRunner(), tmp_path)
    profile = WorkspaceProfile.model_validate(
        {
            "name": "no-planning-worktree",
            "planning": {"required": False},
        }
    )
    adapter = _CaptureWorktreeAdapter()
    hosted_pr_identity = {
        "repo_url": "https://github.com/example/repo.git",
        "pr_number": 778,
        "head_ref": "feature/hosted",
        "expected_head_sha": "a" * 40,
    }

    result = await executor._run_agent_task_with_optional_planning(  # noqa: SLF001
        adapter=adapter,  # type: ignore[arg-type]
        workspace=SimpleNamespace(  # type: ignore[arg-type]
            id="ws_no_plan",
            task_prompt="do it",
            task_tag=None,
            task_policy=None,
        ),
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=worktree,
        model=None,
        hosted_pr_identity=hosted_pr_identity,
    )

    assert result is None
    assert adapter.worktree_paths == [worktree]
    assert adapter.hosted_pr_identities == [hosted_pr_identity]
