"""Hosted post-validation conformance synchronization tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from awf.adapters.base import AgentRunResult
from awf.common.commands import CommandResult
from awf.common.git_identity import git_safe_directory_config_args
from awf.control.executor.planning_conformance import (
    _PostValidationConformanceScopeBaseline,
    _run_post_validation_conformance_check,
)
from awf.control.executor.types import _PlanningValidationHandoff
from awf.db.enums import AgentRuntime
from awf.profiles.models import WorkspaceProfile
from awf.runtime.planning import PlanConformanceReport


class _HostedConformanceAdapter:
    name = AgentRuntime.codex
    is_hosted = True

    def __init__(self, *, terminal_head_sha: str | None, order: list[str]) -> None:
        self.terminal_head_sha = terminal_head_sha
        self.order = order

    async def run(self, **_kwargs: Any) -> AgentRunResult:
        self.order.append("adapter")
        return AgentRunResult(
            returncode=0,
            stdout="",
            stderr="",
            terminal_head_sha=self.terminal_head_sha,
        )


class _HostedSyncRunner:
    def __init__(
        self,
        *,
        context: _HostedConformanceContext,
        worktree_path: Path,
        report_path: Path,
        terminal_head_sha: str,
    ) -> None:
        self.context = context
        self.worktree_path = worktree_path
        self.report_path = report_path
        self.terminal_head_sha = terminal_head_sha
        self.calls: list[list[str]] = []

    async def run(
        self,
        args: list[str],
        *,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        _ = env
        self.calls.append(args)
        if "fetch" in args:
            self.context.order.append("sync-fetch")
            return CommandResult(returncode=0, stdout="", stderr="")
        if args[-2:] == ["rev-parse", "FETCH_HEAD"]:
            self.context.order.append("sync-rev-parse")
            return CommandResult(returncode=0, stdout=f"{self.terminal_head_sha}\n", stderr="")
        if args[-3:] == ["reset", "--hard", self.terminal_head_sha]:
            self.context.order.append("sync-reset")
            self.context.synced = True
            report = self.worktree_path / self.report_path
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(
                (
                    '{"status": "satisfied", "summary": "remote terminal report", '
                    '"reason_code": "CONFORMANCE_SATISFIED", "gaps": []}'
                ),
                encoding="utf-8",
            )
            return CommandResult(returncode=0, stdout="reset\n", stderr="")
        if "restore" in args:
            return CommandResult(returncode=0, stdout="", stderr="")
        return CommandResult(returncode=1, stdout="", stderr=f"unexpected command: {args}")


class _HostedConformanceContext:
    def __init__(self, *, report_path: Path, order: list[str]) -> None:
        self.report_path = report_path
        self.order = order
        self.synced = False
        self.changed_paths_calls = 0
        self.recorded_reports: list[PlanConformanceReport] = []
        self._runner: _HostedSyncRunner

    async def _validation_run_evidence_for_conformance(self, _validation_run_id: str) -> str:
        return "validation passed"

    async def _update_subphase(self, _workspace_id: str, _subphase: str) -> None:
        return None

    async def _changed_paths(self, _worktree_path: Path) -> set[Path]:
        self.order.append("changed-paths")
        assert self.synced, "hosted terminal head must be synchronized before scope reads"
        self.changed_paths_calls += 1
        if self.changed_paths_calls == 1:
            return {self.report_path}
        return set()

    async def _committed_paths_since(self, _worktree_path: Path, _head: str) -> set[Path]:
        assert self.synced, "hosted terminal head must be synchronized before committed diff"
        return {self.report_path}

    def _digest_dirty_content(self, _worktree_path: Path, _paths: set[Path]) -> str:
        raise AssertionError("no pre-dirty paths are expected")

    async def _record_post_validation_conformance_event(
        self,
        *,
        workspace_id: str,
        handoff: _PlanningValidationHandoff,
        report: PlanConformanceReport,
        validation_run_id: str,
    ) -> None:
        _ = workspace_id, handoff, validation_run_id
        self.recorded_reports.append(report)


def _handoff(report_path: Path) -> _PlanningValidationHandoff:
    return _PlanningValidationHandoff(
        report=PlanConformanceReport(
            status="needs_iteration",
            summary="awaiting validation evidence",
            gaps=("missing validation evidence",),
            reason_code="CONFORMANCE_REQUIRES_AWF_VALIDATION",
        ),
        plan_path=Path("docs/awf-plans/ws_hosted.md"),
        report_path=report_path,
        iteration=0,
        max_iterations=2,
    )


@pytest.mark.unit
async def test_hosted_post_validation_conformance_syncs_terminal_head_before_scope_reads(
    tmp_path: Path,
) -> None:
    order: list[str] = []
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()
    report_path = Path("docs/awf-plans/ws_hosted.conformance.json")
    terminal_head_sha = "b" * 40
    context = _HostedConformanceContext(report_path=report_path, order=order)
    context._runner = _HostedSyncRunner(
        context=context,
        worktree_path=worktree_path,
        report_path=report_path,
        terminal_head_sha=terminal_head_sha,
    )
    adapter = _HostedConformanceAdapter(
        terminal_head_sha=terminal_head_sha,
        order=order,
    )

    failure = await _run_post_validation_conformance_check(
        context,
        adapter=adapter,
        workspace=SimpleNamespace(id="ws_hosted", task_prompt="finish the plan"),
        profile=WorkspaceProfile(name="hosted-conformance"),
        compose_project="awf_ws_hosted",
        compose_file=tmp_path / "compose.yml",
        worktree_path=worktree_path,
        model="gpt-5",
        handoff=_handoff(report_path),
        validation_run_id="val_hosted",
        base_commit="a" * 40,
        hosted_pr_identity={
            "head_repo_url": "git@github.com:dimileeh/agent-workspace-fabric.git",
            "head_ref": "awf/ws_hosted",
        },
        conformance_scope_baseline=_PostValidationConformanceScopeBaseline(
            before_compare=set(),
            before_compare_head="a" * 40,
            before_dirty_digests={},
        ),
    )

    assert failure is None
    assert order[:5] == [
        "adapter",
        "sync-fetch",
        "sync-rev-parse",
        "sync-reset",
        "changed-paths",
    ]
    assert context.recorded_reports
    assert context.recorded_reports[0].summary == "remote terminal report"
    assert [
        call
        for call in context._runner.calls
        if call
        == [
            "git",
            *git_safe_directory_config_args(worktree_path),
            "-C",
            str(worktree_path),
            "reset",
            "--hard",
            terminal_head_sha,
        ]
    ]


@pytest.mark.unit
async def test_hosted_post_validation_conformance_sync_failure_fails_closed(
    tmp_path: Path,
) -> None:
    order: list[str] = []
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()
    report_path = Path("docs/awf-plans/ws_hosted.conformance.json")
    terminal_head_sha = "b" * 40
    context = _HostedConformanceContext(report_path=report_path, order=order)
    adapter = _HostedConformanceAdapter(
        terminal_head_sha=terminal_head_sha,
        order=order,
    )

    failure = await _run_post_validation_conformance_check(
        context,
        adapter=adapter,
        workspace=SimpleNamespace(id="ws_hosted", task_prompt="finish the plan"),
        profile=WorkspaceProfile(name="hosted-conformance"),
        compose_project="awf_ws_hosted",
        compose_file=tmp_path / "compose.yml",
        worktree_path=worktree_path,
        model="gpt-5",
        handoff=_handoff(report_path),
        validation_run_id="val_hosted",
        base_commit="a" * 40,
        hosted_pr_identity={},
        conformance_scope_baseline=_PostValidationConformanceScopeBaseline(
            before_compare=set(),
            before_compare_head="a" * 40,
            before_dirty_digests={},
        ),
    )

    assert failure is not None
    assert failure.reason_code == "HOSTED_REMOTE_HEAD_IDENTITY_MISSING"
    assert "terminal head sync failed" in failure.message
    assert failure.details == {
        "hosted_terminal_head_sync": {
            "validation_run_id": "val_hosted",
            "terminal_head_sha": terminal_head_sha,
            "returncode": 1,
            "stdout": "",
            "stderr": "hosted validation fix missing remote PR head identity",
        }
    }
    assert order == ["adapter"]
    assert context.changed_paths_calls == 0


@pytest.mark.unit
async def test_hosted_post_validation_conformance_missing_terminal_head_fails_closed(
    tmp_path: Path,
) -> None:
    order: list[str] = []
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()
    report_path = Path("docs/awf-plans/ws_hosted.conformance.json")
    context = _HostedConformanceContext(report_path=report_path, order=order)
    adapter = _HostedConformanceAdapter(
        terminal_head_sha=None,
        order=order,
    )

    failure = await _run_post_validation_conformance_check(
        context,
        adapter=adapter,
        workspace=SimpleNamespace(id="ws_hosted", task_prompt="finish the plan"),
        profile=WorkspaceProfile(name="hosted-conformance"),
        compose_project="awf_ws_hosted",
        compose_file=tmp_path / "compose.yml",
        worktree_path=worktree_path,
        model="gpt-5",
        handoff=_handoff(report_path),
        validation_run_id="val_hosted",
        base_commit="a" * 40,
        hosted_pr_identity={
            "head_repo_url": "git@github.com:dimileeh/agent-workspace-fabric.git",
            "head_ref": "awf/ws_hosted",
        },
        conformance_scope_baseline=_PostValidationConformanceScopeBaseline(
            before_compare=set(),
            before_compare_head="a" * 40,
            before_dirty_digests={},
        ),
    )

    assert failure is not None
    assert failure.reason_code == "HOSTED_REMOTE_HEAD_MISSING"
    assert "completed without terminal_head_sha" in failure.message
    assert failure.details == {
        "hosted_terminal_head_sync": {
            "validation_run_id": "val_hosted",
            "terminal_head_sha": None,
            "returncode": 1,
            "stdout": "",
            "stderr": "hosted conformance run completed without terminal_head_sha",
        }
    }
    assert order == ["adapter"]
    assert context.changed_paths_calls == 0
