"""Hosted create-path agent terminal-head synchronization regressions."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from awf.adapters.base import AgentRunResult
from awf.common.commands import CommandResult
from awf.control.executor.planning_ops import _run_agent_task_with_optional_planning
from awf.profiles.models import WorkspaceProfile


class _HostedPlanningAdapter:
    is_hosted = True

    def __init__(self, terminal_heads: list[str | None]) -> None:
        self._terminal_heads = terminal_heads
        self.pending_terminal_head: str | None = None

    async def run(self, **_kwargs: Any) -> AgentRunResult:
        assert self.pending_terminal_head is None
        self.pending_terminal_head = self._terminal_heads.pop(0)
        return AgentRunResult(
            returncode=0,
            stdout="",
            stderr="",
            terminal_head_sha=self.pending_terminal_head,
        )


class _HostedPlanningSyncRunner:
    def __init__(
        self,
        *,
        adapter: _HostedPlanningAdapter,
        worktree_path: Path,
        plan_path: Path,
        report_path: Path,
    ) -> None:
        self.adapter = adapter
        self.worktree_path = worktree_path
        self.plan_path = plan_path
        self.report_path = report_path
        self.synced_heads: list[str] = []

    async def run(
        self,
        args: list[str],
        *,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        _ = env
        if args[-2:] == ["rev-parse", "HEAD"]:
            current_head = ["a" * 40, "b" * 40, "c" * 40, "d" * 40][len(self.synced_heads)]
            return CommandResult(returncode=0, stdout=f"{current_head}\n", stderr="")
        terminal_head = self.adapter.pending_terminal_head
        assert terminal_head is not None
        if "fetch" in args:
            return CommandResult(returncode=0, stdout="", stderr="")
        if args[-2:] == ["rev-parse", "FETCH_HEAD"]:
            return CommandResult(returncode=0, stdout=f"{terminal_head}\n", stderr="")
        if args[-3:] == ["reset", "--hard", terminal_head]:
            self.synced_heads.append(terminal_head)
            if len(self.synced_heads) == 1:
                plan = self.worktree_path / self.plan_path
                plan.parent.mkdir(parents=True, exist_ok=True)
                plan.write_text("# Hosted plan\n", encoding="utf-8")
            elif len(self.synced_heads) == 3:
                report = self.worktree_path / self.report_path
                report.write_text(
                    '{"status":"satisfied","summary":"hosted checks passed","gaps":[]}',
                    encoding="utf-8",
                )
            self.adapter.pending_terminal_head = None
            return CommandResult(returncode=0, stdout="reset\n", stderr="")
        raise AssertionError(f"unexpected command: {args}")


class _HostedPlanningContext:
    def __init__(
        self,
        *,
        adapter: _HostedPlanningAdapter,
        worktree_path: Path,
        plan_path: Path,
        report_path: Path,
    ) -> None:
        self.adapter = adapter
        self.plan_path = plan_path
        self.report_path = report_path
        self.heads = ["a" * 40, "b" * 40, "c" * 40, "d" * 40]
        self._runner = _HostedPlanningSyncRunner(
            adapter=adapter,
            worktree_path=worktree_path,
            plan_path=plan_path,
            report_path=report_path,
        )

    def _assert_synchronized(self) -> None:
        assert self.adapter.pending_terminal_head is None, (
            "hosted terminal head must be synchronized before local git state is read"
        )

    async def _update_subphase(self, _workspace_id: str, _subphase: str) -> None:
        return None

    async def _changed_paths(self, _worktree_path: Path) -> set[Path]:
        self._assert_synchronized()
        return set()

    async def _committed_paths_since(self, _worktree_path: Path, baseline: str) -> set[Path]:
        self._assert_synchronized()
        if baseline == self.heads[0]:
            return {self.plan_path}
        return {Path("src/feature.py")}

    async def _git_rev_parse_head(self, _worktree_path: Path) -> str:
        self._assert_synchronized()
        return self.heads[len(self._runner.synced_heads)]

    def _digest_dirty_content(
        self,
        _worktree_path: Path,
        _paths: set[Path],
        *,
        head_sha: str | None = None,
    ) -> str:
        self._assert_synchronized()
        return head_sha or "clean"


def _hosted_identity(initial_head: str) -> dict[str, Any]:
    return {
        "head_repo_url": "git@github.com:example/repo.git",
        "head_ref": "awf/hosted-sync",
        "expected_head_sha": initial_head,
    }


def _planning_profile(*, required: bool) -> WorkspaceProfile:
    return WorkspaceProfile.model_validate(
        {
            "name": "hosted-planning-sync",
            "planning": {
                "required": required,
                "plan_path": "docs/awf-plans/{workspace_id}.md",
                "conformance_report_path": ("docs/awf-plans/{workspace_id}.conformance.json"),
                "max_iterations": 0,
            },
        }
    )


async def _run_hosted_agent_flow(
    *,
    context: _HostedPlanningContext,
    adapter: _HostedPlanningAdapter,
    tmp_path: Path,
    hosted_pr_identity: dict[str, Any],
    planning_required: bool,
) -> Any:
    return await _run_agent_task_with_optional_planning(
        context,
        adapter=adapter,  # type: ignore[arg-type]
        workspace=SimpleNamespace(
            id="ws_hosted",
            task_prompt="implement the hosted plan",
            task_tag=None,
            task_policy=None,
        ),
        profile=_planning_profile(required=planning_required),
        compose_project="awf_ws_hosted",
        compose_file=tmp_path / "compose.yml",
        worktree_path=context._runner.worktree_path,
        model="gpt-5",
        hosted_pr_identity=hosted_pr_identity,
    )


@pytest.mark.unit
async def test_hosted_planning_syncs_every_successful_agent_head_before_local_checks(
    tmp_path: Path,
) -> None:
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()
    plan_path = Path("docs/awf-plans/ws_hosted.md")
    report_path = Path("docs/awf-plans/ws_hosted.conformance.json")
    terminal_heads = ["b" * 40, "c" * 40, "d" * 40]
    adapter = _HostedPlanningAdapter(terminal_heads.copy())
    context = _HostedPlanningContext(
        adapter=adapter,
        worktree_path=worktree_path,
        plan_path=plan_path,
        report_path=report_path,
    )
    hosted_pr_identity = _hosted_identity("a" * 40)

    result = await _run_hosted_agent_flow(
        context=context,
        adapter=adapter,
        tmp_path=tmp_path,
        hosted_pr_identity=hosted_pr_identity,
        planning_required=True,
    )

    assert result is None
    assert context._runner.synced_heads == terminal_heads
    assert hosted_pr_identity["expected_head_sha"] == terminal_heads[-1]


@pytest.mark.unit
async def test_hosted_non_planning_run_syncs_terminal_head_before_return(
    tmp_path: Path,
) -> None:
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()
    terminal_head = "b" * 40
    adapter = _HostedPlanningAdapter([terminal_head])
    context = _HostedPlanningContext(
        adapter=adapter,
        worktree_path=worktree_path,
        plan_path=Path("docs/awf-plans/unused.md"),
        report_path=Path("docs/awf-plans/unused.conformance.json"),
    )
    hosted_pr_identity = _hosted_identity("a" * 40)

    result = await _run_hosted_agent_flow(
        context=context,
        adapter=adapter,
        tmp_path=tmp_path,
        hosted_pr_identity=hosted_pr_identity,
        planning_required=False,
    )

    assert result is None
    assert context._runner.synced_heads == [terminal_head]
    assert hosted_pr_identity["expected_head_sha"] == terminal_head


@pytest.mark.unit
@pytest.mark.parametrize(
    ("missing_invocation", "expected_phase", "expected_synced_count"),
    [
        (0, "planning", 0),
        (1, "implementation", 1),
        (2, "conformance", 2),
    ],
)
async def test_hosted_planning_missing_terminal_head_fails_before_local_phase_checks(
    tmp_path: Path,
    missing_invocation: int,
    expected_phase: str,
    expected_synced_count: int,
) -> None:
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()
    plan_path = Path("docs/awf-plans/ws_hosted.md")
    report_path = Path("docs/awf-plans/ws_hosted.conformance.json")
    terminal_heads: list[str | None] = ["b" * 40, "c" * 40, "d" * 40]
    terminal_heads[missing_invocation] = None
    adapter = _HostedPlanningAdapter(terminal_heads)
    context = _HostedPlanningContext(
        adapter=adapter,
        worktree_path=worktree_path,
        plan_path=plan_path,
        report_path=report_path,
    )

    result = await _run_hosted_agent_flow(
        context=context,
        adapter=adapter,
        tmp_path=tmp_path,
        hosted_pr_identity=_hosted_identity("a" * 40),
        planning_required=True,
    )

    assert result is not None
    assert result.reason_code == "HOSTED_REMOTE_HEAD_MISSING"
    assert f"hosted {expected_phase} terminal head sync failed" in result.message
    assert context._runner.synced_heads == ["b" * 40, "c" * 40][:expected_synced_count]


@pytest.mark.unit
async def test_hosted_non_planning_missing_terminal_head_fails_before_return(
    tmp_path: Path,
) -> None:
    adapter = _HostedPlanningAdapter([None])
    context = _HostedPlanningContext(
        adapter=adapter,
        worktree_path=tmp_path,
        plan_path=Path("docs/awf-plans/unused.md"),
        report_path=Path("docs/awf-plans/unused.conformance.json"),
    )

    result = await _run_hosted_agent_flow(
        context=context,
        adapter=adapter,
        tmp_path=tmp_path,
        hosted_pr_identity=_hosted_identity("a" * 40),
        planning_required=False,
    )

    assert result is not None
    assert result.reason_code == "HOSTED_REMOTE_HEAD_MISSING"
    assert "hosted agent terminal head sync failed" in result.message
    assert context._runner.synced_heads == []


@pytest.mark.unit
async def test_hosted_planning_sync_failure_is_returned_before_scope_reads(
    tmp_path: Path,
) -> None:
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()
    adapter = _HostedPlanningAdapter(["b" * 40])
    context = _HostedPlanningContext(
        adapter=adapter,
        worktree_path=worktree_path,
        plan_path=Path("docs/awf-plans/ws_hosted.md"),
        report_path=Path("docs/awf-plans/ws_hosted.conformance.json"),
    )

    result = await _run_hosted_agent_flow(
        context=context,
        adapter=adapter,
        tmp_path=tmp_path,
        hosted_pr_identity={},
        planning_required=True,
    )

    assert result is not None
    assert result.reason_code == "HOSTED_REMOTE_HEAD_IDENTITY_MISSING"
    assert "hosted planning terminal head sync failed" in result.message
    assert result.details == {
        "hosted_terminal_head_sync": {
            "phase": "planning",
            "terminal_head_sha": "b" * 40,
            "returncode": 1,
            "stdout": "",
            "stderr": "hosted validation fix missing remote PR head identity",
        }
    }
