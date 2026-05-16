"""Failure-analysis metrics API tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from awf.db.enums import FailureReason, WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.planning import (
    AGENT_PLAN_PHASE_SCOPE_VIOLATION,
    PLAN_CONFORMANCE_UNSATISFIED,
)


async def _workspace(
    engine: AsyncEngine,
    *,
    status: WorkspaceStatus,
    updated_at: datetime,
    failure_reason: FailureReason | str | None = None,
    repo_url: str = "git@github.com:example/failures-api.git",
    branch_base: str = "main",
    task_title: str | None = None,
    agent: str = "codex",
    failure_message: str | None = None,
    pr_url: str | None = None,
    task_policy: dict | None = None,
) -> str:
    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await WorkspaceRepository(session).create(
            repo_url=repo_url,
            branch_base=branch_base,
            task_title=task_title or f"{status.value} workspace",
            task_prompt="Collect failure analysis metrics.",
            agent=agent,
            test_commands=[],
        )
        workspace.status = status.value
        workspace.updated_at = updated_at
        workspace.failure_reason = (
            failure_reason.value if isinstance(failure_reason, FailureReason) else failure_reason
        )
        workspace.failure_message = failure_message
        workspace.pr_url = pr_url
        if task_policy is not None:
            workspace.task_policy = task_policy
        await session.commit()
        return workspace.id


async def _conformance_failed_workspace(engine: AsyncEngine, *, updated_at: datetime) -> str:
    factory = make_session_factory(engine)
    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.create(
            repo_url="git@github.com:example/failures-api.git",
            branch_base="main",
            task_title="Finish plan conformance",
            task_prompt="Finish the saved plan.",
            agent="codex",
            test_commands=[],
        )
        await repo.transition(workspace, to=WorkspaceStatus.provisioning, reason_code="TEST")
        workspace.failure_reason = FailureReason.agent_failure.value
        workspace.failure_message = "plan conformance was not satisfied"
        await repo.transition(
            workspace,
            to=WorkspaceStatus.failed,
            reason_code=PLAN_CONFORMANCE_UNSATISFIED,
            payload={
                "details": {
                    "conformance": {
                        "summary": "Still missing work.",
                        "gaps": ["Add focused retry test"],
                        "reason_code": PLAN_CONFORMANCE_UNSATISFIED,
                        "iterations_used": 0,
                        "max_iterations": 0,
                        "plan_path": "docs/awf-plans/ws.md",
                        "report_path": "docs/awf-plans/ws.conformance.json",
                    }
                },
                "salvage": {"branch_name": "awf/ws_conformance"},
            },
        )
        workspace.updated_at = updated_at
        await session.commit()
        return workspace.id


async def _planning_scope_failed_workspace(engine: AsyncEngine, *, updated_at: datetime) -> str:
    factory = make_session_factory(engine)
    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.create(
            repo_url="git@github.com:example/failures-api.git",
            branch_base="main",
            task_title="Reject premature planning work",
            task_prompt="Create the plan only.",
            agent="codex",
            test_commands=[],
        )
        await repo.transition(workspace, to=WorkspaceStatus.provisioning, reason_code="TEST")
        workspace.failure_reason = FailureReason.agent_failure.value
        workspace.failure_message = "planning phase changed files outside plan artifact"
        await repo.transition(
            workspace,
            to=WorkspaceStatus.failed,
            reason_code=AGENT_PLAN_PHASE_SCOPE_VIOLATION,
            payload={
                "details": {
                    "planning_scope": {
                        "scope_phase": "planning",
                        "required_paths": ["docs/awf-plans/ws_scope.md"],
                        "offending_paths": ["src/awf/runtime/planning.py"],
                        "offending_commands": [],
                        "recommended_action": (
                            "Retry planning from a clean workspace and salvage the "
                            "preserved branch only after explicit operator approval."
                        ),
                        "recovery_strategy": "discard_and_replan",
                        "salvage_policy": "explicit_salvage_required",
                    },
                    "recommended_action": (
                        "Retry planning from a clean workspace and salvage the preserved "
                        "branch only after explicit operator approval."
                    ),
                    "recovery_strategy": "discard_and_replan",
                    "salvage_policy": "explicit_salvage_required",
                },
                "salvage": {"branch_name": "awf/ws_scope"},
            },
        )
        workspace.updated_at = updated_at
        await session.commit()
        return workspace.id


async def _provider_capacity_exhausted_workspace(
    engine: AsyncEngine, *, updated_at: datetime
) -> str:
    factory = make_session_factory(engine)
    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.create(
            repo_url="git@github.com:example/failures-api.git",
            branch_base="main",
            task_title="Provider capacity test",
            task_prompt="Try to hit API.",
            agent="gemini",
            test_commands=[],
        )
        await repo.transition(workspace, to=WorkspaceStatus.provisioning, reason_code="TEST")
        workspace.failure_reason = FailureReason.agent_failure.value
        workspace.failure_message = "quota exhausted"
        await repo.transition(
            workspace,
            to=WorkspaceStatus.failed,
            reason_code="AGENT_PROVIDER_CAPACITY_EXHAUSTED",
            payload={
                "details": {
                    "provider": "google",
                    "model": "gemini-1.5-pro",
                    "retryable": True,
                    "recommended_action": "Retry the workspace later or fallback to a different provider.",
                }
            },
        )
        workspace.updated_at = updated_at
        await session.commit()
        return workspace.id


@pytest.mark.unit
async def test_failure_summary_endpoint_returns_console_payload(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    now = datetime.now(UTC)
    await _workspace(
        engine,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(hours=3),
        failure_reason=FailureReason.infrastructure_failure,
    )
    missing_reason_id = await _workspace(
        engine,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(minutes=2),
        task_title="No reason captured",
        failure_message="worker exited unexpectedly",
    )
    validation_id = await _workspace(
        engine,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(minutes=5),
        failure_reason=FailureReason.validation_failure,
        repo_url="git@github.com:example/product.git",
        branch_base="development",
        task_title="Fix checkout bug",
        agent="gemini",
        failure_message="npm test failed",
        pr_url="https://github.com/example/product/pull/7",
    )
    await _workspace(
        engine,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(minutes=1),
        failure_reason=FailureReason.agent_failure,
    )

    response = await client.get("/v1/metrics/failures/summary", params={"since_hours": 2})

    assert response.status_code == 200
    body = response.json()
    generated_at = datetime.fromisoformat(body["generated_at"])
    window_start = datetime.fromisoformat(body["window_start"])
    assert window_start == generated_at - timedelta(hours=2)
    assert body["since_hours"] == 2
    assert body["total_failed_workspaces"] == 2
    assert body["failure_groups"] == [
        {
            "failure_reason": "unknown",
            "count": 1,
            "retryable": False,
            "recommended_action": (
                "Inspect workspace logs and classify the failure_reason before retrying."
            ),
        },
        {
            "failure_reason": FailureReason.validation_failure.value,
            "count": 1,
            "retryable": False,
            "recommended_action": (
                "Review validation output and fix failing checks before retrying."
            ),
        },
    ]
    assert [example["workspace_id"] for example in body["latest_examples"]] == [
        missing_reason_id,
        validation_id,
    ]
    validation_example = body["latest_examples"][1]
    assert validation_example["title"] == "Fix checkout bug"
    assert validation_example["repo_url"] == "git@github.com:example/product.git"
    assert validation_example["branch_base"] == "development"
    assert validation_example["agent"] == "gemini"
    assert validation_example["status"] == WorkspaceStatus.failed.value
    assert validation_example["failure_reason"] == FailureReason.validation_failure.value
    assert validation_example["failure_message"] == "npm test failed"
    assert validation_example["pr_url"] == "https://github.com/example/product/pull/7"


@pytest.mark.unit
async def test_failure_summary_endpoint_accepts_example_limit(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    now = datetime.now(UTC)
    workspace_ids: list[str] = []
    for index in range(6):
        workspace_ids.append(
            await _workspace(
                engine,
                status=WorkspaceStatus.failed,
                updated_at=now - timedelta(minutes=index),
                failure_reason=FailureReason.infrastructure_failure,
                task_title=f"Infrastructure failure {index}",
            )
        )

    response = await client.get(
        "/v1/metrics/failures/summary",
        params={"limit": 6},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total_failed_workspaces"] == 6
    assert [example["workspace_id"] for example in body["latest_examples"]] == workspace_ids


@pytest.mark.unit
@pytest.mark.parametrize("since_hours", ["0", "169"])
async def test_failure_summary_validates_since_hours_bounds(
    client: AsyncClient,
    since_hours: str,
) -> None:
    response = await client.get(
        "/v1/metrics/failures/summary",
        params={"since_hours": since_hours},
    )

    assert response.status_code == 422


@pytest.mark.unit
@pytest.mark.parametrize("limit", ["0", "26"])
async def test_failure_summary_validates_limit_bounds(
    client: AsyncClient,
    limit: str,
) -> None:
    response = await client.get(
        "/v1/metrics/failures/summary",
        params={"limit": limit},
    )

    assert response.status_code == 422


@pytest.mark.unit
async def test_api_route_serialization(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    from awf.db.enums import FailureReason, WorkspaceStatus

    now = datetime.now(UTC)

    workspace = await _workspace(
        engine,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(hours=1),
        failure_reason=FailureReason.validation_failure,
        failure_message="missing managed worktree during fix loop",
        agent="gemini",
        task_policy={"agent_model": "gemini-1.5-pro"},
    )

    response = await client.get("/v1/metrics/failures/summary")

    assert response.status_code == 200
    body = response.json()

    assert "root_cause_clusters" in body
    clusters = body["root_cause_clusters"]
    assert len(clusters) == 1
    cluster = clusters[0]

    assert cluster["agent"] == "gemini"
    assert cluster["agent_model"] == "gemini-1.5-pro"
    assert cluster["failure_reason"] == "validation_failure"
    assert cluster["likely_cause"] == "Missing Managed Worktree"
    assert cluster["actionable_next_action"] == "Review fix loop configuration or git identity"
    assert cluster["count"] == 1
    assert workspace in cluster["sample_workspace_ids"]


@pytest.mark.unit
async def test_failure_summary_endpoint_exposes_conformance_details(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    now = datetime.now(UTC)
    workspace_id = await _conformance_failed_workspace(
        engine,
        updated_at=now - timedelta(minutes=3),
    )

    response = await client.get("/v1/metrics/failures/summary")

    assert response.status_code == 200
    body = response.json()
    assert body["latest_examples"][0]["workspace_id"] == workspace_id
    assert body["latest_examples"][0]["reason_code"] == PLAN_CONFORMANCE_UNSATISFIED
    assert body["latest_examples"][0]["details"]["conformance"]["gaps"] == [
        "Add focused retry test"
    ]
    assert body["latest_examples"][0]["salvage"] == {"branch_name": "awf/ws_conformance"}
    assert body["root_cause_clusters"][0]["reason_code"] == PLAN_CONFORMANCE_UNSATISFIED
    assert body["root_cause_clusters"][0]["likely_cause"] == "Plan Conformance Unsatisfied"


@pytest.mark.unit
async def test_failure_summary_endpoint_exposes_planning_scope_violation(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    now = datetime.now(UTC)
    workspace_id = await _planning_scope_failed_workspace(
        engine,
        updated_at=now - timedelta(minutes=3),
    )

    response = await client.get("/v1/metrics/failures/summary")

    assert response.status_code == 200
    body = response.json()
    example = next(item for item in body["latest_examples"] if item["workspace_id"] == workspace_id)
    assert example["reason_code"] == AGENT_PLAN_PHASE_SCOPE_VIOLATION
    assert example["details"]["planning_scope"]["offending_paths"] == [
        "src/awf/runtime/planning.py"
    ]
    assert example["details"]["salvage_policy"] == "explicit_salvage_required"
    assert example["salvage"] == {"branch_name": "awf/ws_scope"}
    cluster = next(
        item
        for item in body["root_cause_clusters"]
        if item["reason_code"] == AGENT_PLAN_PHASE_SCOPE_VIOLATION
    )
    assert cluster["likely_cause"] == "Planning Scope Violation"
    assert cluster["actionable_next_action"] == (
        "Retry planning from a clean workspace; salvage the preserved branch only "
        "after explicit operator approval."
    )


@pytest.mark.unit
async def test_failure_summary_endpoint_exposes_provider_capacity_exhausted(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    now = datetime.now(UTC)
    workspace_id = await _provider_capacity_exhausted_workspace(
        engine,
        updated_at=now - timedelta(minutes=1),
    )

    response = await client.get("/v1/metrics/failures/summary")

    assert response.status_code == 200
    body = response.json()

    # Find our cluster
    cluster = next(
        c
        for c in body["root_cause_clusters"]
        if c["reason_code"] == "AGENT_PROVIDER_CAPACITY_EXHAUSTED"
    )
    assert cluster["likely_cause"] == "Provider Quota Exhausted"
    assert (
        cluster["actionable_next_action"]
        == "Retry the workspace later or fallback to a different provider."
    )

    # Find our example in latest_examples
    example = next(e for e in body["latest_examples"] if e["workspace_id"] == workspace_id)
    assert example["reason_code"] == "AGENT_PROVIDER_CAPACITY_EXHAUSTED"
    assert example["details"]["provider"] == "google"
    assert example["details"]["model"] == "gemini-1.5-pro"
    assert example["details"]["retryable"] is True
    assert (
        example["details"]["recommended_action"]
        == "Retry the workspace later or fallback to a different provider."
    )
