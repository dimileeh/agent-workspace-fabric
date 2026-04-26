"""Merge queue visualization API tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory


async def _create_queue_workspace(
    engine: AsyncEngine,
    *,
    title: str,
    status: WorkspaceStatus,
    pr_url: str | None,
    repo_url: str = "git@github.com:example/console.git",
    base_branch: str = "main",
    branch_name: str = "codex/merge-queue",
    auto_merge: bool = True,
    task_class: str | None = "test_task",
    owned_paths: list[str] | None = None,
    resolved_profile: dict | None = None,
    updated_at: datetime | None = None,
) -> str:
    from awf.db.repositories import MergeCandidateRepository, TaskAttemptRepository, TaskRepository

    factory = make_session_factory(engine)
    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.create(
            repo_url=repo_url,
            branch_base=base_branch,
            task_title=title,
            task_prompt=f"Implement {title}.",
            task_external_id=None,
            task_class=task_class,
            owned_paths=["src/awf/api/**"] if owned_paths is None else owned_paths,
            auto_merge=auto_merge,
            agent=AgentRuntime.codex.value,
            resolved_profile=resolved_profile,
            test_commands=["pytest -q"],
        )
        workspace.status = status.value
        workspace.branch_name = branch_name
        workspace.pr_url = pr_url
        workspace.pr_number = int(pr_url.rstrip("/").split("/")[-1]) if pr_url is not None else None
        if updated_at is not None:
            workspace.updated_at = updated_at
        task = await TaskRepository(session).create_or_get(
            repo_url=repo_url,
            base_branch=base_branch,
            title=title,
            prompt=f"Implement {title}.",
            external_id=f"QUEUE-{title}",
            idempotency_key=None,
            task_class=task_class,
            owned_paths=["src/awf/api/**"] if owned_paths is None else owned_paths,
        )
        attempt = await TaskAttemptRepository(session).create_for_workspace(
            task=task,
            workspace=workspace,
        )
        if pr_url is not None:
            attempt.is_canonical_for_merge = True
            candidate_repo = MergeCandidateRepository(session)
            await candidate_repo.create_or_update_open_for_attempt(
                task=task,
                attempt=attempt,
                workspace=workspace,
                head_sha="head123",
                base_sha="base123",
            )
            if status == WorkspaceStatus.completed:
                await candidate_repo.mark_workspace_merged(workspace.id)
            elif status in {WorkspaceStatus.failed, WorkspaceStatus.cancelled}:
                await candidate_repo.close_open_for_workspace(
                    workspace.id,
                    close_reason=f"WORKSPACE_{status.value.upper()}",
                )
        await repo.add_event(
            workspace,
            event_type="merge_queue.test_marker",
            reason_code="TEST",
            payload={"title": title},
        )
        await session.commit()
        return workspace.id


async def _refresh_scope_policy(
    engine: AsyncEngine,
    *,
    workspace_id: str,
    changed_paths: tuple[str, ...],
) -> None:
    from sqlalchemy import select

    from awf.db.models import MergeCandidate
    from awf.service.scope_policy import ScopePolicyRefreshService

    factory = make_session_factory(engine)
    async with factory() as session:
        candidate_id = (
            await session.execute(
                select(MergeCandidate.id).where(MergeCandidate.workspace_id == workspace_id)
            )
        ).scalar_one()
        await ScopePolicyRefreshService(session).refresh_candidate(
            candidate_id,
            changed_paths=changed_paths,
        )
        await session.commit()


async def _attempt_id_for_workspace(engine: AsyncEngine, workspace_id: str) -> str:
    factory = make_session_factory(engine)
    async with factory() as session:
        result = await session.execute(
            text("SELECT id FROM task_attempts WHERE workspace_id = :workspace_id"),
            {"workspace_id": workspace_id},
        )
        attempt_id = result.scalar_one()
        assert isinstance(attempt_id, str)
        return attempt_id


async def _insert_validation_run(
    engine: AsyncEngine,
    *,
    run_id: str,
    workspace_id: str,
    attempt_id: str,
    target_head_sha: str,
    status: str = "succeeded",
    finished_at: datetime = datetime(2026, 4, 26, 12, 5, tzinfo=UTC),
) -> None:
    factory = make_session_factory(engine)
    async with factory() as session:
        await session.execute(
            text(
                """
                INSERT INTO validation_runs (
                    id,
                    workspace_id,
                    attempt_id,
                    tier,
                    command_set_hash,
                    commands,
                    base_commit,
                    target_branch,
                    target_head_sha,
                    status,
                    reason_code,
                    started_at,
                    finished_at,
                    log_stream_refs
                )
                VALUES (
                    :id,
                    :workspace_id,
                    :attempt_id,
                    1,
                    :command_set_hash,
                    :commands,
                    'base123',
                    'codex/merge-queue',
                    :target_head_sha,
                    :status,
                    'VALIDATION_OK',
                    :started_at,
                    :finished_at,
                    :log_stream_refs
                )
                """
            ),
            {
                "id": run_id,
                "workspace_id": workspace_id,
                "attempt_id": attempt_id,
                "target_head_sha": target_head_sha,
                "status": status,
                "command_set_hash": "b" * 64,
                "commands": json.dumps(
                    [
                        {
                            "phase": "validate",
                            "command_index": 1,
                            "command": "pytest -q",
                            "stream_ids": {
                                "stdout": "validation.01_validate.stdout",
                                "stderr": "validation.01_validate.stderr",
                            },
                        }
                    ]
                ),
                "started_at": datetime(2026, 4, 26, 12, 0, tzinfo=UTC),
                "finished_at": finished_at,
                "log_stream_refs": json.dumps(
                    {
                        "commands": [
                            {
                                "stdout": "validation.01_validate.stdout",
                                "stderr": "validation.01_validate.stderr",
                            }
                        ]
                    }
                ),
            },
        )
        await session.commit()


class TestMergeQueueList:
    @pytest.mark.unit
    async def test_lists_active_pr_workspaces_newest_updated_first_with_required_shape(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
    ) -> None:
        older_id = await _create_queue_workspace(
            engine,
            title="Older monitored PR",
            status=WorkspaceStatus.monitoring_pr,
            pr_url="https://github.com/example/console/pull/1",
            updated_at=datetime(2026, 4, 20, 12, 0, tzinfo=UTC),
        )
        newer_id = await _create_queue_workspace(
            engine,
            title="Newer completed PR",
            status=WorkspaceStatus.completed,
            pr_url="https://github.com/example/console/pull/2",
            branch_name="codex/completed",
            updated_at=datetime(2026, 4, 21, 12, 0, tzinfo=UTC),
        )
        await _create_queue_workspace(
            engine,
            title="No PR yet",
            status=WorkspaceStatus.monitoring_pr,
            pr_url=None,
            updated_at=datetime(2026, 4, 22, 12, 0, tzinfo=UTC),
        )

        response = await client.get("/v1/merge-queue")

        assert response.status_code == 200
        body = response.json()
        assert body["next_cursor"] is None
        assert body["has_more"] is False
        assert [item["workspace_id"] for item in body["items"]] == [older_id]

        item = body["items"][0]
        assert set(item) == {
            "candidate_id",
            "candidate_status",
            "close_reason",
            "attempt_id",
            "task_id",
            "workspace_id",
            "title",
            "repo_url",
            "base_branch",
            "branch_name",
            "pr_url",
            "status",
            "auto_merge",
            "task_class",
            "owned_paths",
            "created_at",
            "updated_at",
            "last_event",
            "merge_blocker_reason",
            "required_next_action",
            "readiness",
            "canonical",
            "latest_validation",
            "stale_reasons",
            "policy_findings",
        }
        assert item["stale_reasons"] == []
        assert item["policy_findings"] == []
        assert item["candidate_id"].startswith("mc_")
        assert item["candidate_status"] == "open"
        assert item["attempt_id"].startswith("att_")
        assert item["task_id"].startswith("task_")
        assert item["title"] == "Older monitored PR"
        assert item["repo_url"] == "git@github.com:example/console.git"
        assert item["base_branch"] == "main"
        assert item["branch_name"] == "codex/merge-queue"
        assert item["pr_url"] == "https://github.com/example/console/pull/1"
        assert item["status"] == WorkspaceStatus.monitoring_pr.value
        assert item["auto_merge"] is True
        assert item["task_class"] == "test_task"
        assert item["owned_paths"] == ["src/awf/api/**"]
        assert item["last_event"]["event_type"] == "merge_queue.test_marker"
        assert item["merge_blocker_reason"] == "ready_to_merge_or_waiting_for_github"
        assert item["canonical"] is True
        assert item["readiness"] == {
            "ready": True,
            "manual_merge_required": False,
            "waiting_for_monitor": False,
            "failed_or_cancelled": False,
            "completed": False,
            "not_canonical": False,
            "stale": False,
            "stale_reason": None,
        }
        assert item["latest_validation"] is None
        assert newer_id not in {row["workspace_id"] for row in body["items"]}

    @pytest.mark.unit
    async def test_filters_by_repo_base_status_and_limit(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
    ) -> None:
        expected_id = await _create_queue_workspace(
            engine,
            title="Expected",
            status=WorkspaceStatus.monitoring_pr,
            repo_url="git@github.com:example/console.git",
            base_branch="development",
            pr_url="https://github.com/example/console/pull/3",
            updated_at=datetime(2026, 4, 23, 12, 0, tzinfo=UTC),
        )
        await _create_queue_workspace(
            engine,
            title="Older matching row beyond limit",
            status=WorkspaceStatus.monitoring_pr,
            repo_url="git@github.com:example/console.git",
            base_branch="development",
            pr_url="https://github.com/example/console/pull/4",
            updated_at=datetime(2026, 4, 22, 12, 0, tzinfo=UTC),
        )
        await _create_queue_workspace(
            engine,
            title="Wrong repo",
            status=WorkspaceStatus.monitoring_pr,
            repo_url="git@github.com:example/api.git",
            base_branch="development",
            pr_url="https://github.com/example/api/pull/5",
            updated_at=datetime(2026, 4, 24, 12, 0, tzinfo=UTC),
        )
        await _create_queue_workspace(
            engine,
            title="Wrong base branch",
            status=WorkspaceStatus.monitoring_pr,
            repo_url="git@github.com:example/console.git",
            base_branch="main",
            pr_url="https://github.com/example/console/pull/6",
            updated_at=datetime(2026, 4, 25, 12, 0, tzinfo=UTC),
        )
        await _create_queue_workspace(
            engine,
            title="Wrong status",
            status=WorkspaceStatus.completed,
            repo_url="git@github.com:example/console.git",
            base_branch="development",
            pr_url="https://github.com/example/console/pull/7",
            updated_at=datetime(2026, 4, 26, 12, 0, tzinfo=UTC),
        )

        response = await client.get(
            "/v1/merge-queue",
            params={
                "repo_url": "git@github.com:example/console.git",
                "base_branch": "development",
                "status": WorkspaceStatus.monitoring_pr.value,
                "limit": 1,
            },
        )

        assert response.status_code == 200
        assert [item["workspace_id"] for item in response.json()["items"]] == [expected_id]

    @pytest.mark.unit
    async def test_reports_has_more_and_accepts_next_cursor(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
    ) -> None:
        older_id = await _create_queue_workspace(
            engine,
            title="Older monitored PR",
            status=WorkspaceStatus.monitoring_pr,
            pr_url="https://github.com/example/console/pull/8",
            updated_at=datetime(2026, 4, 22, 12, 0, tzinfo=UTC),
        )
        newer_id = await _create_queue_workspace(
            engine,
            title="Newer monitored PR",
            status=WorkspaceStatus.monitoring_pr,
            pr_url="https://github.com/example/console/pull/9",
            updated_at=datetime(2026, 4, 23, 12, 0, tzinfo=UTC),
        )

        first_response = await client.get("/v1/merge-queue", params={"limit": 1})

        assert first_response.status_code == 200
        first_body = first_response.json()
        assert [item["workspace_id"] for item in first_body["items"]] == [newer_id]
        assert first_body["has_more"] is True
        assert first_body["next_cursor"] is not None

        second_response = await client.get(
            "/v1/merge-queue",
            params={"limit": 1, "cursor": first_body["next_cursor"]},
        )

        assert second_response.status_code == 200
        second_body = second_response.json()
        assert [item["workspace_id"] for item in second_body["items"]] == [older_id]
        assert second_body["has_more"] is False
        assert second_body["next_cursor"] is None

    @pytest.mark.unit
    async def test_rejects_invalid_cursor(self, client: AsyncClient) -> None:
        response = await client.get("/v1/merge-queue", params={"cursor": "not-a-cursor"})

        assert response.status_code == 400
        assert response.json()["detail"]["error_code"] == "INVALID_CURSOR"

    @pytest.mark.unit
    @pytest.mark.parametrize("limit", [0, 501])
    async def test_validates_limit_bounds(self, client: AsyncClient, limit: int) -> None:
        response = await client.get("/v1/merge-queue", params={"limit": limit})

        assert response.status_code == 422

    @pytest.mark.unit
    async def test_derives_blocker_reasons_for_active_candidates(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
    ) -> None:
        rows = [
            (
                "auto monitor",
                WorkspaceStatus.monitoring_pr,
                True,
                "ready_to_merge_or_waiting_for_github",
            ),
            ("manual monitor", WorkspaceStatus.monitoring_pr, False, "manual_merge_required"),
            ("pushing", WorkspaceStatus.pushing, True, "waiting_for_monitor"),
            ("validating", WorkspaceStatus.validating, True, "workspace_not_terminal"),
            ("completed", WorkspaceStatus.completed, True, "completed"),
            ("failed", WorkspaceStatus.failed, True, "failed_or_cancelled"),
            ("cancelled", WorkspaceStatus.cancelled, True, "failed_or_cancelled"),
        ]
        expected: dict[str, str] = {}
        for index, (title, status, auto_merge, reason) in enumerate(rows):
            workspace_id = await _create_queue_workspace(
                engine,
                title=title,
                status=status,
                auto_merge=auto_merge,
                pr_url=f"https://github.com/example/console/pull/{index + 10}",
                updated_at=datetime(2026, 4, 20 + index, 12, 0, tzinfo=UTC),
            )
            if status not in {
                WorkspaceStatus.completed,
                WorkspaceStatus.failed,
                WorkspaceStatus.cancelled,
            }:
                expected[workspace_id] = reason

        response = await client.get("/v1/merge-queue", params={"limit": 20})

        assert response.status_code == 200
        actual = {
            item["workspace_id"]: item["merge_blocker_reason"] for item in response.json()["items"]
        }
        assert actual == expected

    @pytest.mark.unit
    async def test_returns_candidate_backed_readiness_while_preserving_workspace_fields(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
    ) -> None:
        workspace_id = await _create_queue_workspace(
            engine,
            title="Candidate readiness",
            status=WorkspaceStatus.monitoring_pr,
            pr_url="https://github.com/example/console/pull/21",
            branch_name="awf/candidate-readiness",
            updated_at=datetime(2026, 4, 26, 12, 0, tzinfo=UTC),
        )

        response = await client.get("/v1/merge-queue")

        assert response.status_code == 200
        item = next(
            item for item in response.json()["items"] if item["workspace_id"] == workspace_id
        )
        assert item["workspace_id"] == workspace_id
        assert item["status"] == WorkspaceStatus.monitoring_pr.value
        assert item["candidate_status"] == "open"
        assert item["canonical"] is True
        assert item["readiness"] == {
            "ready": True,
            "manual_merge_required": False,
            "waiting_for_monitor": False,
            "failed_or_cancelled": False,
            "completed": False,
            "not_canonical": False,
            "stale": False,
            "stale_reason": None,
        }
        assert item["merge_blocker_reason"] == "ready_to_merge_or_waiting_for_github"

    @pytest.mark.unit
    async def test_blocking_out_of_scope_findings_block_merge_queue_readiness(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
    ) -> None:
        workspace_id = await _create_queue_workspace(
            engine,
            title="Blocking policy finding",
            status=WorkspaceStatus.monitoring_pr,
            pr_url="https://github.com/example/console/pull/23",
            owned_paths=["src/owned/**"],
            resolved_profile={
                "quality": {"out_of_scope_changes": {"mode": "block"}},
            },
            updated_at=datetime(2026, 4, 26, 12, 0, tzinfo=UTC),
        )
        await _refresh_scope_policy(
            engine,
            workspace_id=workspace_id,
            changed_paths=("src/unowned.py",),
        )

        response = await client.get("/v1/merge-queue")

        assert response.status_code == 200
        item = next(
            item for item in response.json()["items"] if item["workspace_id"] == workspace_id
        )
        assert item["readiness"]["ready"] is False
        assert item["merge_blocker_reason"] == "policy_blocked"
        assert item["required_next_action"] == "resolve_policy_findings"
        assert item["policy_findings"][0]["reason_code"] == "OUT_OF_SCOPE_CHANGE"
        assert item["policy_findings"][0]["severity"] == "blocking"
        assert item["policy_findings"][0]["subject_path"] == "src/unowned.py"

    @pytest.mark.unit
    async def test_warning_out_of_scope_findings_are_visible_but_do_not_block(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
    ) -> None:
        workspace_id = await _create_queue_workspace(
            engine,
            title="Warning policy finding",
            status=WorkspaceStatus.monitoring_pr,
            pr_url="https://github.com/example/console/pull/24",
            owned_paths=["src/owned/**"],
            updated_at=datetime(2026, 4, 26, 12, 0, tzinfo=UTC),
        )
        await _refresh_scope_policy(
            engine,
            workspace_id=workspace_id,
            changed_paths=("docs/extra.md",),
        )

        queue_response = await client.get("/v1/merge-queue")
        detail_response = await client.get(f"/v1/workspaces/{workspace_id}")

        assert queue_response.status_code == 200
        item = next(
            item
            for item in queue_response.json()["items"]
            if item["workspace_id"] == workspace_id
        )
        assert item["readiness"]["ready"] is True
        assert item["merge_blocker_reason"] == "ready_to_merge_or_waiting_for_github"
        assert item["required_next_action"] is None
        assert item["policy_findings"][0]["severity"] == "warning"
        assert item["policy_findings"][0]["subject_path"] == "docs/extra.md"

        assert detail_response.status_code == 200
        detail = detail_response.json()
        assert detail["policy_findings"][0]["reason_code"] == "OUT_OF_SCOPE_CHANGE"
        assert detail["policy_findings"][0]["severity"] == "warning"
        assert detail["policy_findings"][0]["subject_path"] == "docs/extra.md"

    @pytest.mark.unit
    async def test_exposes_latest_validation_provenance_and_freshness(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
    ) -> None:
        workspace_id = await _create_queue_workspace(
            engine,
            title="Validation provenance",
            status=WorkspaceStatus.monitoring_pr,
            pr_url="https://github.com/example/console/pull/22",
            branch_name="codex/merge-queue",
            updated_at=datetime(2026, 4, 26, 12, 0, tzinfo=UTC),
        )
        attempt_id = await _attempt_id_for_workspace(engine, workspace_id)
        await _insert_validation_run(
            engine,
            run_id="vr_222222222222222222222222",
            workspace_id=workspace_id,
            attempt_id=attempt_id,
            target_head_sha="old-head",
        )

        response = await client.get("/v1/merge-queue")

        assert response.status_code == 200
        item = next(
            item for item in response.json()["items"] if item["workspace_id"] == workspace_id
        )
        assert item["latest_validation"] == {
            "validation_run_id": "vr_222222222222222222222222",
            "attempt_id": attempt_id,
            "tier": 1,
            "command_set_hash": "b" * 64,
            "base_commit": "base123",
            "target_branch": "codex/merge-queue",
            "target_head_sha": "old-head",
            "current_target_head_sha": "head123",
            "status": "succeeded",
            "reason_code": "VALIDATION_OK",
            "started_at": "2026-04-26T12:00:00Z",
            "finished_at": "2026-04-26T12:05:00Z",
            "log_stream_refs": {
                "commands": [
                    {
                        "stdout": "validation.01_validate.stdout",
                        "stderr": "validation.01_validate.stderr",
                    }
                ]
            },
            "fresh_for_target": False,
        }
