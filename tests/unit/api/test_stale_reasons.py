"""API surface tests for stale reasons.

Stale reasons must be visible to console clients via:
- ``/v1/merge-queue`` items (list of structured reasons + boolean readiness.stale)
- ``/v1/workspaces/{id}/stale-reasons`` workspace detail endpoint
- ``workspace_events`` rows so the existing event timeline shows them
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.repositories import (
    MergeCandidateRepository,
    TaskAttemptRepository,
    TaskRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory


@pytest.fixture
async def factory(engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    yield make_session_factory(engine)


async def _seed_candidate(
    factory: async_sessionmaker[AsyncSession],
    *,
    pr_url: str = "https://github.com/example/svc/pull/77",
    pr_number: int = 77,
    branch_name: str = "awf/stale-test",
    owned_paths: list[str] | None = None,
    task_class: str | None = "refactor_task",
    base_sha: str = "a" * 40,
    updated_at: datetime | None = None,
) -> tuple[str, str, str]:
    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.create(
            repo_url="git@github.com:example/svc.git",
            branch_base="development",
            task_title="Stale visibility",
            task_prompt="Implement.",
            task_external_id="TICKET-VISIBLE",
            agent=AgentRuntime.codex.value,
            test_commands=[],
            owned_paths=owned_paths or ["src/awf/api/**"],
            task_class=task_class,
        )
        task = await TaskRepository(session).create_or_get(
            repo_url=workspace.repo_url,
            base_branch=workspace.branch_base,
            title=workspace.task_title,
            prompt=workspace.task_prompt,
            external_id=workspace.task_external_id,
            idempotency_key=None,
            task_class=task_class,
            owned_paths=owned_paths or ["src/awf/api/**"],
        )
        attempt = await TaskAttemptRepository(session).create_for_workspace(
            task=task,
            workspace=workspace,
        )
        for target in (
            WorkspaceStatus.provisioning,
            WorkspaceStatus.ready,
            WorkspaceStatus.running,
            WorkspaceStatus.validating,
            WorkspaceStatus.pushing,
        ):
            await repo.transition(workspace, to=target, reason_code="TEST")
        workspace.branch_name = branch_name
        workspace.remote_push_branch = branch_name
        workspace.base_commit = base_sha
        workspace.pr_url = pr_url
        workspace.pr_number = pr_number
        await repo.transition(
            workspace,
            to=WorkspaceStatus.monitoring_pr,
            reason_code="PR_OPENED",
        )
        attempt = await TaskAttemptRepository(session).get_by_workspace_id(workspace.id)
        assert attempt is not None
        candidate = await MergeCandidateRepository(session).create_or_update_open_for_attempt(
            task=task,
            attempt=attempt,
            workspace=workspace,
            head_sha="h" * 40,
            base_sha=base_sha,
        )
        if updated_at is not None:
            workspace.updated_at = updated_at
        await session.commit()
        return workspace.id, attempt.id, candidate.id


class TestMergeQueueExposesStaleReasons:
    @pytest.mark.unit
    async def test_merge_queue_item_includes_active_stale_reasons_after_refresh(
        self,
        client: AsyncClient,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from awf.service.staleness import (
            StalenessRefreshService,
            TargetBranchState,
        )

        workspace_id, _attempt_id, candidate_id = await _seed_candidate(factory)

        async with factory() as session:
            service = StalenessRefreshService(session)
            await service.refresh_candidate(
                candidate_id,
                target=TargetBranchState(
                    branch="development",
                    head_sha="b" * 40,
                    changed_paths=("src/awf/api/routes/health.py",),
                    advanced_commits=2,
                ),
            )
            await session.commit()

        response = await client.get("/v1/merge-queue")
        assert response.status_code == 200
        items = response.json()["items"]
        item = next(it for it in items if it["workspace_id"] == workspace_id)
        assert item["readiness"]["stale"] is True
        assert item["merge_blocker_reason"] == "stale"

        reasons = item["stale_reasons"]
        assert isinstance(reasons, list)
        assert reasons, "expected at least one stale reason exposed"
        first = reasons[0]
        assert {
            "id",
            "reason_code",
            "trigger_type",
            "trigger_ref",
            "explanation",
            "status",
            "detected_at",
            "resolved_at",
        } <= set(first.keys())
        assert first["status"] == "active"
        assert first["reason_code"] in {
            "STALE_OVERLAP",
            "STALE_TARGET_ADVANCED",
        }

    @pytest.mark.unit
    async def test_merge_queue_item_uses_sensitive_stale_reason_for_rebase_action(
        self,
        client: AsyncClient,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from awf.service.staleness import (
            StalenessRefreshService,
            TargetBranchState,
        )

        workspace_id, _attempt_id, candidate_id = await _seed_candidate(
            factory,
            pr_url="https://github.com/example/svc/pull/177",
            pr_number=177,
            branch_name="awf/dependency-stale",
            owned_paths=["src/awf/service/**"],
            task_class="dependency_task",
        )

        async with factory() as session:
            service = StalenessRefreshService(session)
            await service.refresh_candidate(
                candidate_id,
                target=TargetBranchState(
                    branch="development",
                    head_sha="b" * 40,
                    changed_paths=("uv.lock",),
                    advanced_commits=1,
                ),
            )
            await session.commit()

        response = await client.get("/v1/merge-queue")
        assert response.status_code == 200
        item = next(it for it in response.json()["items"] if it["workspace_id"] == workspace_id)

        assert item["readiness"]["stale"] is True
        assert item["merge_blocker_reason"] == "stale"
        assert item["required_next_action"] == "rebase"
        assert [r["reason_code"] for r in item["stale_reasons"]] == ["STALE_DEPENDENCY"]

    @pytest.mark.unit
    async def test_docs_class_with_non_overlapping_change_keeps_mergeable(
        self,
        client: AsyncClient,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from awf.service.staleness import (
            StalenessRefreshService,
            TargetBranchState,
        )

        workspace_id, _attempt_id, candidate_id = await _seed_candidate(
            factory,
            pr_url="https://github.com/example/svc/pull/78",
            pr_number=78,
            branch_name="awf/docs-only",
            owned_paths=["docs/USAGE.md"],
            task_class="docs_task",
        )

        async with factory() as session:
            service = StalenessRefreshService(session)
            await service.refresh_candidate(
                candidate_id,
                target=TargetBranchState(
                    branch="development",
                    head_sha="b" * 40,
                    changed_paths=("src/awf/api/routes/health.py",),
                    advanced_commits=2,
                ),
            )
            await session.commit()

        response = await client.get("/v1/merge-queue")
        assert response.status_code == 200
        items = response.json()["items"]
        item = next(it for it in items if it["workspace_id"] == workspace_id)
        assert item["readiness"]["stale"] is False
        assert item["merge_blocker_reason"] == "ready_to_merge_or_waiting_for_github"
        assert item["stale_reasons"] == []


class TestWorkspaceStaleReasonsEndpoint:
    @pytest.mark.unit
    async def test_workspace_stale_reasons_endpoint_lists_active_reasons(
        self,
        client: AsyncClient,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from awf.service.staleness import (
            StalenessRefreshService,
            TargetBranchState,
        )

        workspace_id, _attempt_id, candidate_id = await _seed_candidate(
            factory,
            pr_url="https://github.com/example/svc/pull/79",
            pr_number=79,
        )

        async with factory() as session:
            service = StalenessRefreshService(session)
            await service.refresh_candidate(
                candidate_id,
                target=TargetBranchState(
                    branch="development",
                    head_sha="c" * 40,
                    changed_paths=("src/awf/api/routes/locks.py",),
                    advanced_commits=4,
                ),
            )
            await session.commit()

        response = await client.get(f"/v1/workspaces/{workspace_id}/stale-reasons")
        assert response.status_code == 200
        body = response.json()
        assert "items" in body
        assert any(r["status"] == "active" for r in body["items"])
        assert all(r["workspace_id"] == workspace_id for r in body["items"])

    @pytest.mark.unit
    async def test_workspace_stale_reasons_endpoint_includes_resolved_when_requested(
        self,
        client: AsyncClient,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from awf.service.staleness import (
            StalenessRefreshService,
            TargetBranchState,
        )

        workspace_id, _attempt_id, candidate_id = await _seed_candidate(
            factory,
            pr_url="https://github.com/example/svc/pull/179",
            pr_number=179,
            branch_name="awf/resolved-stale",
            owned_paths=["src/awf/api/**"],
            task_class="docs_task",
        )

        async with factory() as session:
            service = StalenessRefreshService(session)
            await service.refresh_candidate(
                candidate_id,
                target=TargetBranchState(
                    branch="development",
                    head_sha="b" * 40,
                    changed_paths=("src/awf/api/routes/locks.py",),
                    advanced_commits=1,
                ),
            )
            await service.refresh_candidate(
                candidate_id,
                target=TargetBranchState(
                    branch="development",
                    head_sha="a" * 40,
                    changed_paths=(),
                    advanced_commits=0,
                ),
            )
            await session.commit()

        active_response = await client.get(f"/v1/workspaces/{workspace_id}/stale-reasons")
        assert active_response.status_code == 200
        assert active_response.json()["items"] == []

        resolved_response = await client.get(
            f"/v1/workspaces/{workspace_id}/stale-reasons",
            params={"include_resolved": "true"},
        )
        assert resolved_response.status_code == 200
        items = resolved_response.json()["items"]
        assert [item["reason_code"] for item in items] == ["STALE_OVERLAP"]
        assert items[0]["status"] == "resolved"
        assert items[0]["resolved_at"] is not None

    @pytest.mark.unit
    async def test_workspace_stale_reasons_endpoint_returns_empty_for_unknown_workspace(
        self,
        client: AsyncClient,
    ) -> None:
        response = await client.get("/v1/workspaces/ws_does_not_exist/stale-reasons")
        assert response.status_code == 404
