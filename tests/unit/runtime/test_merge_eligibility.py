import pytest

from awf.db.enums import TaskClass, WorkspaceStatus
from awf.db.models import MergeCandidate, TaskAttempt, Workspace
from awf.db.repositories import _sync_candidate_readiness


@pytest.mark.asyncio
async def test_merge_candidate_requires_sufficient_validation_tier() -> None:
    # Set up mock or real db objects
    workspace = Workspace(
        id="ws_1",
        status=WorkspaceStatus.monitoring_pr.value,
        auto_merge=True,
        task_class=TaskClass.refactor_task.value,
    )
    attempt = TaskAttempt(
        id="att_1",
        agent="claude_code",
        is_canonical_for_merge=True,
    )
    candidate = MergeCandidate(
        id="mc_1",
        workspace=workspace,
        attempt=attempt,
        status="open",
        stale=False,
    )

    # In refactor_task, require tier 2. If no validation provenance exists, should be stale.
    # We will pass them to _sync_candidate_readiness, maybe it queries validation history?
    # Or maybe a new component computes it. Let's just assert candidate is NOT ready.
    _sync_candidate_readiness(candidate, workspace=workspace, attempt=attempt)

    assert candidate.ready is False
    assert candidate.stale is True
    # And there should be a stale reason exposed
    assert hasattr(candidate, "stale_reason")
    assert candidate.stale_reason == "validation_insufficient_tier"


@pytest.mark.asyncio
async def test_merge_candidate_clears_stale_state_when_computed_reason_clears() -> None:
    workspace = Workspace(
        id="ws_1",
        status=WorkspaceStatus.monitoring_pr.value,
        auto_merge=True,
        task_class=TaskClass.test_task.value,
    )
    attempt = TaskAttempt(
        id="att_1",
        agent="claude_code",
        is_canonical_for_merge=True,
    )
    candidate = MergeCandidate(
        id="mc_1",
        workspace=workspace,
        attempt=attempt,
        status="open",
        stale=True,
        stale_reason="previous_computed_reason",
    )

    _sync_candidate_readiness(candidate, workspace=workspace, attempt=attempt)

    assert candidate.ready is True
    assert candidate.stale is False
    assert candidate.stale_reason is None
