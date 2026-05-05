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
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm.attributes import set_committed_value

from awf.db.enums import AgentRuntime, OperationStatus, OperationType, WorkspaceStatus
from awf.db.repositories import (
    MergeCandidateRepository,
    OperationRepository,
    StaleReasonCreate,
    StaleReasonRepository,
    TaskAttemptRepository,
    TaskRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.service.workspace_observability import DEFAULT_STALE_REASON_LIMIT


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
    successful_validate_tier: int | None = None,
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
        if successful_validate_tier is not None:
            operation = await OperationRepository(session).create(
                workspace_id=workspace.id,
                operation_type=OperationType.validate,
                status=OperationStatus.succeeded,
                payload={"requested_tier": successful_validate_tier},
            )
            set_committed_value(workspace, "operations", [operation])
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
            "severity",
            "blocks_merge",
        } <= set(first.keys())
        assert first["status"] == "active"
        assert first["severity"] == "blocking"
        assert first["blocks_merge"] is True
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
            successful_validate_tier=2,
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
    async def test_workspace_stale_reasons_endpoint_exposes_owned_path_overlap_reason(
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
            pr_url="https://github.com/example/svc/pull/279",
            pr_number=279,
            branch_name="awf/owned-path-overlap",
            owned_paths=["src/awf/service/**"],
            task_class="test_task",
        )

        async with factory() as session:
            service = StalenessRefreshService(session)
            await service.refresh_candidate(
                candidate_id,
                target=TargetBranchState(
                    branch="development",
                    head_sha="b" * 40,
                    changed_paths=("src/awf/service/staleness.py",),
                    advanced_commits=1,
                ),
            )
            await session.commit()

        response = await client.get(f"/v1/workspaces/{workspace_id}/stale-reasons")

        assert response.status_code == 200
        body = response.json()
        assert body["next_cursor"] is None
        assert body["has_more"] is False
        assert body["cursor"] is None
        items = body["items"]
        assert body["limit"] == DEFAULT_STALE_REASON_LIMIT
        assert len(items) == 1
        assert set(items[0]) == {
            "id",
            "workspace_id",
            "candidate_id",
            "attempt_id",
            "task_id",
            "trigger_type",
            "trigger_ref",
            "reason_code",
            "explanation",
            "status",
            "severity",
            "blocks_merge",
            "detected_at",
            "resolved_at",
        }
        assert items[0]["workspace_id"] == workspace_id
        assert items[0]["candidate_id"] == candidate_id
        assert items[0]["reason_code"] == "STALE_OVERLAP"
        assert items[0]["trigger_type"] == "path_overlap"
        assert items[0]["trigger_ref"] == "src/awf/service/staleness.py"
        assert items[0]["severity"] == "blocking"
        assert items[0]["blocks_merge"] is True
        assert items[0]["status"] == "active"
        assert items[0]["detected_at"] is not None
        assert items[0]["resolved_at"] is None

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
        assert body["next_cursor"] is None
        assert body["has_more"] is False
        assert body["limit"] == DEFAULT_STALE_REASON_LIMIT
        assert body["cursor"] is None
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
    async def test_workspace_stale_reasons_next_cursor_fetches_second_page(
        self,
        client: AsyncClient,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        workspace_id, attempt_id, candidate_id = await _seed_candidate(
            factory,
            pr_url="https://github.com/example/svc/pull/379",
            pr_number=379,
            branch_name="awf/stale-pagination",
        )

        async with factory() as session:
            candidate = await MergeCandidateRepository(session).get_by_attempt_id(attempt_id)
            assert candidate is not None
            assert candidate.id == candidate_id
            await StaleReasonRepository(session).replace_active_findings(
                workspace_id=workspace_id,
                candidate_id=candidate_id,
                attempt_id=attempt_id,
                task_id=candidate.task_id,
                findings=[
                    StaleReasonCreate(
                        reason_code="STALE_DEPENDENCY",
                        trigger_type="dependency_changed",
                        trigger_ref="uv.lock",
                        explanation="Dependency manifest changed on target branch.",
                    ),
                    StaleReasonCreate(
                        reason_code="STALE_SCHEMA",
                        trigger_type="schema_changed",
                        trigger_ref="migrations/versions/new.py",
                        explanation="Schema lineage changed on target branch.",
                    ),
                ],
            )
            await session.commit()

        first_response = await client.get(
            f"/v1/workspaces/{workspace_id}/stale-reasons",
            params={"limit": 1},
        )

        assert first_response.status_code == 200
        first_page = first_response.json()
        assert len(first_page["items"]) == 1
        assert first_page["has_more"] is True
        assert first_page["next_cursor"] is not None

        second_response = await client.get(
            f"/v1/workspaces/{workspace_id}/stale-reasons",
            params={"limit": 1, "cursor": first_page["next_cursor"]},
        )

        assert second_response.status_code == 200
        second_page = second_response.json()
        assert len(second_page["items"]) == 1
        assert {
            first_page["items"][0]["reason_code"],
            second_page["items"][0]["reason_code"],
        } == {"STALE_DEPENDENCY", "STALE_SCHEMA"}
        assert second_page["has_more"] is False
        assert second_page["next_cursor"] is None
        assert second_page["cursor"] == first_page["next_cursor"]

    @pytest.mark.unit
    async def test_workspace_stale_reasons_page_query_is_bounded(
        self,
        client: AsyncClient,
        factory: async_sessionmaker[AsyncSession],
        engine: AsyncEngine,
    ) -> None:
        workspace_id, attempt_id, candidate_id = await _seed_candidate(
            factory,
            pr_url="https://github.com/example/svc/pull/380",
            pr_number=380,
            branch_name="awf/stale-query-pagination",
        )

        async with factory() as session:
            candidate = await MergeCandidateRepository(session).get_by_attempt_id(attempt_id)
            assert candidate is not None
            await StaleReasonRepository(session).replace_active_findings(
                workspace_id=workspace_id,
                candidate_id=candidate_id,
                attempt_id=attempt_id,
                task_id=candidate.task_id,
                findings=[
                    StaleReasonCreate(
                        reason_code="STALE_DEPENDENCY",
                        trigger_type="dependency_changed",
                        trigger_ref="uv.lock",
                        explanation="Dependency manifest changed on target branch.",
                    ),
                    StaleReasonCreate(
                        reason_code="STALE_SCHEMA",
                        trigger_type="schema_changed",
                        trigger_ref="migrations/versions/new.py",
                        explanation="Schema lineage changed on target branch.",
                    ),
                ],
            )
            await session.commit()

        first_response = await client.get(
            f"/v1/workspaces/{workspace_id}/stale-reasons",
            params={"limit": 1},
        )
        assert first_response.status_code == 200
        first_page = first_response.json()
        assert first_page["next_cursor"] is not None

        stale_reason_selects: list[str] = []

        def capture_stale_reason_select(
            _conn: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            normalized = " ".join(statement.lower().split())
            if "select" in normalized and "from stale_reasons" in normalized:
                stale_reason_selects.append(normalized)

        event.listen(
            engine.sync_engine,
            "before_cursor_execute",
            capture_stale_reason_select,
        )
        try:
            second_response = await client.get(
                f"/v1/workspaces/{workspace_id}/stale-reasons",
                params={"limit": 1, "cursor": first_page["next_cursor"]},
            )
        finally:
            event.remove(
                engine.sync_engine,
                "before_cursor_execute",
                capture_stale_reason_select,
            )

        assert second_response.status_code == 200
        assert stale_reason_selects
        assert any(
            " limit " in statement and " offset " in statement for statement in stale_reason_selects
        )

    @pytest.mark.unit
    async def test_workspace_stale_reasons_endpoint_returns_empty_for_unknown_workspace(
        self,
        client: AsyncClient,
    ) -> None:
        response = await client.get("/v1/workspaces/ws_does_not_exist/stale-reasons")
        assert response.status_code == 404
