"""Regression: post-agent semantic precommit repair forwards hosted context.

Hosted agent start payloads derive sanitized profile/docker mode and
Postgres env-file handling from ``request.profile`` / ``request.worktree_path``.
The repair path must pass those through ``adapter.run`` the same way initial,
fix, and conformance runs already do.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from awf.adapters.base import AgentRunResult
from awf.common.commands import CommandResult
from awf.control.executor import quality_methods
from awf.control.executor.quality_gates import _PostAgentCommitClassification
from awf.db.enums import WorkspaceStatus
from awf.profiles.models import WorkspaceProfile


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


@pytest.mark.unit
async def test_semantic_precommit_repair_forwards_profile_and_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repair agent.run must receive profile + worktree_path for hosted Cloud."""
    captured: dict[str, Any] = {}
    profile = WorkspaceProfile(name="precommit-repair-hosted")
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services: {}\n", encoding="utf-8")

    async def _capture_run(**kwargs: Any) -> AgentRunResult:
        captured.update(kwargs)
        return AgentRunResult(returncode=0, stdout="repair ok", stderr="")

    adapter = SimpleNamespace(run=_capture_run)
    workspace = SimpleNamespace(
        id="ws_precommit_hosted",
        owned_paths=[],
        status=WorkspaceStatus.running.value,
    )

    async def _invoke_run_agent(
        self: Any,
        *,
        run_agent: Any,
        **_kwargs: Any,
    ) -> tuple[bool, Any]:
        return True, await run_agent(False)

    monkeypatch.setattr(
        quality_methods,
        "_run_agent_callable_with_service_recovery",
        _invoke_run_agent,
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

    async def _git_in_worktree(args: list[str]) -> CommandResult:
        if args[:1] == ["add"]:
            return CommandResult(returncode=0, stdout="", stderr="")
        if args[:3] == ["diff", "--cached", "--name-only"]:
            return CommandResult(returncode=0, stdout="src/app.py\n", stderr="")
        return CommandResult(returncode=0, stdout="", stderr="")

    result = await quality_methods._run_post_agent_semantic_precommit_repair(  # noqa: SLF001
        self_obj,
        workspace_id="ws_precommit_hosted",
        worktree_path=worktree_path,
        base_commit="abc123",
        commit_result=CommandResult(returncode=1, stdout="hook failed", stderr=""),
        classification=_semantic_classification(),
        staged_paths=["src/app.py"],
        run_commit=AsyncMock(
            return_value=CommandResult(returncode=0, stdout="committed", stderr="")
        ),
        git_in_worktree=_git_in_worktree,
        adapter=adapter,  # type: ignore[arg-type]
        compose_project="awf_ws_precommit_hosted",
        compose_file=compose_file,
        model=None,
        ws=workspace,  # type: ignore[arg-type]
        profile=profile,
        command_evidence=[],
    )

    assert result is True
    assert captured["profile"] is profile
    assert captured["worktree_path"] == worktree_path
    assert captured["log_source"] == "post_agent_precommit_repair"
    assert captured["workspace_id"] == "ws_precommit_hosted"
