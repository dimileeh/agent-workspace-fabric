"""Monitor-origin blocked guide candidate edge cases."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from awf.db.enums import WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import MergeCandidateRepository, TaskAttemptRepository, TaskRepository
from tests.postgres import postgres_test_session
from tests.unit.service.test_controls_lifecycle_parts.controls_lifecycle_helpers import (
    _events,
    _service,
    _workspace,
)


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with postgres_test_session() as s:
        yield s


async def _monitor_origin_blocked_workspace(session: AsyncSession) -> Workspace:
    workspace = await _workspace(session, status=WorkspaceStatus.blocked)
    workspace.block_reason_code = "QUALITY_GATE_POLICY_CHANGED"
    workspace.block_type = "protected_quality_gate"
    workspace.block_epoch = 1
    workspace.block_violations = [
        {"path": "pyproject.toml", "section": "tool.coverage", "line": 5, "reason": "weakened"}
    ]
    workspace.block_resume_phase = "monitor_protected_scope_push"
    workspace.pr_url = "https://github.com/example/control-lifecycle/pull/7"
    workspace.pr_number = 7
    workspace.branch_name = f"awf/{workspace.id}"
    workspace.remote_push_branch = workspace.branch_name
    workspace.base_commit = "a" * 40
    workspace.monitor_last_commit_sha = "h" * 40
    await session.flush()
    return workspace


@pytest.mark.unit
async def test_guide_monitor_origin_blocked_with_attempt_without_candidate(
    session: AsyncSession,
) -> None:
    # A monitor-origin block can have an attempt row before a merge candidate is
    # available. Guiding it should still resume monitoring and report that no
    # candidate was reopened rather than failing or fabricating one.
    workspace = await _monitor_origin_blocked_workspace(session)
    task = await TaskRepository(session).create_or_get(
        repo_url=workspace.repo_url,
        base_branch=workspace.branch_base,
        title=workspace.task_title,
        prompt=workspace.task_prompt,
        external_id=workspace.task_external_id,
        idempotency_key=None,
        task_class=workspace.task_class,
        owned_paths=list(workspace.owned_paths),
    )
    await TaskAttemptRepository(session).create_for_workspace(task=task, workspace=workspace)
    await session.flush()
    service, _stopper, _cleaner = _service(session)

    await service.guide_workspace(
        workspace.id,
        directive="re-check the PR without reopening a candidate",
        reason="operator resolved the protected-scope block",
        idempotency_key="guide-monitor-attempt-no-candidate",
        expected_version=workspace.version,
    )

    assert workspace.status == WorkspaceStatus.monitoring_pr.value
    assert (
        await MergeCandidateRepository(session).get_open_for_workspace_with_merge_inputs(
            workspace.id
        )
        is None
    )
    events = await _events(session, workspace.id)
    guide_event = next(e for e in events if e.event_type == "workspace.guide_requested")
    assert guide_event.payload["state_reset"]["candidate_reopened"] is False
