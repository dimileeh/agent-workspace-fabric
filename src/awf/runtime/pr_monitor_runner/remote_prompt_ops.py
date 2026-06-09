"""Prompt and progress helpers for protected-scope remote repair."""

from __future__ import annotations

from typing import Any

from awf.control.quality_gates import QualityGateViolation
from awf.db.repositories import WorkspaceRepository
from awf.runtime.pr_monitor import (
    MonitorState,
    PRStatus,
    sync_base_no_progress_signature,
)
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult


async def _protected_scope_repair_prompt(
    self: Any,
    *,
    workspace_id: str,
    violations: list[QualityGateViolation],
) -> str:
    paths, owned = await self._protected_scope_prompt_sections(
        workspace_id=workspace_id,
        violations=violations,
    )
    return (
        "Your previous PR-monitor repair changed protected file(s) outside "
        "this workspace's declared owned_paths.\n\n"
        f"Protected out-of-scope changes:\n{paths}\n\n"
        f"Declared owned_paths:\n{owned}\n\n"
        "Do not commit or keep these protected out-of-scope edits. Remove "
        "the protected-file changes from the worktree and resolve the PR "
        "feedback using files inside the declared scope. For example, if a "
        "review offers either changing CI or making a test self-sufficient, "
        "prefer the in-scope test change when CI workflow files are not "
        "owned. If the protected change is truly required, leave it removed "
        "and explain that human/orchestrator approval is needed.\n\n"
        "Make the smallest corrective edit now. AWF will re-check the diff "
        "before it commits or pushes."
    )


async def _protected_scope_committed_repair_prompt(
    self: Any,
    *,
    workspace_id: str,
    violations: list[QualityGateViolation],
) -> str:
    paths, owned = await self._protected_scope_prompt_sections(
        workspace_id=workspace_id,
        violations=violations,
    )
    return (
        "Your previous PR-monitor repair committed protected file(s) outside "
        "this workspace's declared owned_paths.\n\n"
        f"Protected out-of-scope changes already committed locally:\n{paths}\n\n"
        f"Declared owned_paths:\n{owned}\n\n"
        "These protected edits are already committed locally in the branch diff "
        "AWF is about to push. Remove the protected-file changes from branch "
        "history relative to the PR head by making a reverting commit or "
        "rewriting local commits, then keep the PR feedback resolution inside "
        "the declared scope. Do not only clean the worktree; the committed diff "
        "must no longer contain these protected paths. If the protected change "
        "is truly required, remove it from the branch history and explain that "
        "human/orchestrator approval is needed.\n\n"
        "Make the smallest corrective edit now. AWF will re-check the committed "
        "diff before it pushes."
    )


async def _protected_scope_prompt_sections(
    self: Any,
    *,
    workspace_id: str,
    violations: list[QualityGateViolation],
) -> tuple[str, str]:
    async with self._deps.session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        owned_paths = list(workspace.owned_paths) if workspace is not None else []
    paths = "\n".join(
        "  - "
        f"{violation.path} :: {violation.section or violation.protected_pattern} :: "
        f"line {violation.line if violation.line is not None else 'unknown'} :: "
        f"{violation.reason}"
        for violation in violations
    )
    owned = "\n".join(f"  - {path}" for path in owned_paths) or "  - (none declared)"
    return paths, owned


def _record_sync_base_progress(
    self: Any,
    *,
    state: MonitorState,
    status: PRStatus,
    push_result: _GitPushResult,
) -> None:
    _ = self
    if push_result.pushed or push_result.failed:
        state.sync_base_no_progress_signature = None
        state.sync_base_no_progress_count = 0
        return
    signature = sync_base_no_progress_signature(status)
    if state.sync_base_no_progress_signature == signature:
        state.sync_base_no_progress_count += 1
    else:
        state.sync_base_no_progress_signature = signature
        state.sync_base_no_progress_count = 1
