"""Merge queue legacy terminal-exclusion regression tests (G1, G5)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory


async def _create_legacy_queue_workspace(
    engine: AsyncEngine,
    *,
    title: str,
    status: WorkspaceStatus,
    pr_url: str,
    auto_merge: bool = True,
    updated_at: datetime | None = None,
) -> str:
    factory = make_session_factory(engine)
    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.create(
            repo_url="git@github.com:example/legacy.git",
            branch_base="main",
            task_title=title,
            task_prompt=f"Implement {title}.",
            task_external_id=f"LEGACY-{title}",
            task_class="test_task",
            owned_paths=["legacy/**"],
            auto_merge=auto_merge,
            agent=AgentRuntime.codex.value,
            test_commands=["pytest -q"],
        )
        workspace.status = status.value
        workspace.branch_name = f"codex/{title.lower().replace(' ', '-')}"
        workspace.pr_url = pr_url
        workspace.pr_number = int(pr_url.rstrip("/").split("/")[-1])
        if updated_at is not None:
            workspace.updated_at = updated_at
        await repo.add_event(
            workspace,
            event_type="merge_queue.legacy_marker",
            reason_code="TEST",
            payload={"title": title},
        )
        await session.commit()
        return workspace.id


class TestMergeQueueLegacyTerminalExclusion:
    @pytest.mark.unit
    @pytest.mark.parametrize(
        "status",
        [
            WorkspaceStatus.failed,
            WorkspaceStatus.completed,
            WorkspaceStatus.cancelled,
        ],
    )
    async def test_pr_bearing_terminal_legacy_workspace_absent_from_merge_queue(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
        status: WorkspaceStatus,
    ) -> None:
        """G1: PR-bearing terminal workspace with no MergeCandidate is omitted."""
        workspace_id = await _create_legacy_queue_workspace(
            engine,
            title=f"Terminal {status.value}",
            status=status,
            pr_url=f"https://github.com/example/legacy/pull/{700 + hash(status.value) % 100}",
            updated_at=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
        )

        response = await client.get(
            "/v1/merge-queue",
            params={"repo_url": "git@github.com:example/legacy.git", "limit": 50},
        )

        assert response.status_code == 200
        workspace_ids = {item["workspace_id"] for item in response.json()["items"]}
        assert workspace_id not in workspace_ids

    @pytest.mark.unit
    async def test_non_terminal_legacy_boundary_statuses_remain_visible(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
    ) -> None:
        """G5: blocked, pushing, monitoring_pr legacy rows remain in the queue."""
        expected: dict[str, WorkspaceStatus] = {}
        for index, (title, status, auto_merge) in enumerate(
            [
                ("Blocked legacy boundary", WorkspaceStatus.blocked, True),
                ("Pushing legacy boundary", WorkspaceStatus.pushing, True),
                ("Monitoring legacy boundary", WorkspaceStatus.monitoring_pr, True),
                (
                    "Manual monitoring legacy boundary",
                    WorkspaceStatus.monitoring_pr,
                    False,
                ),
            ]
        ):
            workspace_id = await _create_legacy_queue_workspace(
                engine,
                title=title,
                status=status,
                auto_merge=auto_merge,
                pr_url=f"https://github.com/example/legacy/pull/{800 + index}",
                updated_at=datetime(2026, 5, 2, index, 0, tzinfo=UTC),
            )
            expected[workspace_id] = status

        response = await client.get(
            "/v1/merge-queue",
            params={"repo_url": "git@github.com:example/legacy.git", "limit": 50},
        )

        assert response.status_code == 200
        items = {item["workspace_id"]: item for item in response.json()["items"]}
        assert set(expected) <= set(items)
        for workspace_id, status in expected.items():
            assert items[workspace_id]["candidate_id"] is None
            assert items[workspace_id]["status"] == status.value
