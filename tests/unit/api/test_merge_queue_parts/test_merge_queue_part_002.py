"""Merge queue visualization API tests."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.orm.attributes import set_committed_value

import awf.service.merge_queue as merge_queue_service
from awf.db.enums import AgentRuntime, OperationStatus, OperationType, WorkspaceStatus
from awf.db.models import MergeCandidate
from awf.db.repositories import (
    OperationRepository,
    StaleReasonCreate,
    StaleReasonRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.runtime.merge_eligibility import VALIDATION_INSUFFICIENT_TIER_STALE_REASON


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
    candidate_created_at: datetime | None = None,
    successful_validate_tier: int | None = None,
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
            if successful_validate_tier is not None:
                operation = await OperationRepository(session).create(
                    workspace_id=workspace.id,
                    operation_type=OperationType.validate,
                    status=OperationStatus.succeeded,
                    payload={"requested_tier": successful_validate_tier},
                )
                set_committed_value(workspace, "operations", [operation])
            candidate_repo = MergeCandidateRepository(session)
            candidate = await candidate_repo.create_or_update_open_for_attempt(
                task=task,
                attempt=attempt,
                workspace=workspace,
                head_sha="head123",
                base_sha="base123",
            )
            if candidate_created_at is not None:
                candidate.created_at = candidate_created_at
                candidate.updated_at = candidate_created_at
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


def _encoded_cursor(payload: dict[str, object]) -> str:
    return base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")


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


async def _candidate_id_for_workspace(engine: AsyncEngine, workspace_id: str) -> str:
    factory = make_session_factory(engine)
    async with factory() as session:
        result = await session.execute(
            text("SELECT id FROM merge_candidates WHERE workspace_id = :workspace_id"),
            {"workspace_id": workspace_id},
        )
        candidate_id = result.scalar_one()
        assert isinstance(candidate_id, str)
        return candidate_id


async def _add_active_stale_reason(
    engine: AsyncEngine,
    *,
    workspace_id: str,
    reason_code: str = "STALE_DEPENDENCY",
) -> None:
    factory = make_session_factory(engine)
    async with factory() as session:
        result = await session.execute(
            text(
                """
                SELECT id, attempt_id, task_id
                FROM merge_candidates
                WHERE workspace_id = :workspace_id
                """
            ),
            {"workspace_id": workspace_id},
        )
        row = result.one()
        await StaleReasonRepository(session).replace_active_findings(
            workspace_id=workspace_id,
            candidate_id=row.id,
            attempt_id=row.attempt_id,
            task_id=row.task_id,
            findings=[
                StaleReasonCreate(
                    reason_code=reason_code,
                    trigger_type="dependency_changed",
                    trigger_ref="uv.lock",
                    explanation="Dependency manifest changed on target branch.",
                )
            ],
        )
        await session.commit()


async def _add_monitor_recovery_operation(engine: AsyncEngine, workspace_id: str) -> None:
    factory = make_session_factory(engine)
    async with factory() as session:
        await OperationRepository(session).create(
            workspace_id=workspace_id,
            operation_type=OperationType.validate,
            status=OperationStatus.pending,
            payload={"source": "pr_monitor", "reason": "validation_insufficient_tier"},
        )
        await session.commit()


async def _add_operation(
    engine: AsyncEngine,
    workspace_id: str,
    *,
    operation_type: OperationType,
    status: OperationStatus,
    created_at: datetime,
    payload: dict[str, object] | None = None,
) -> None:
    factory = make_session_factory(engine)
    async with factory() as session:
        operation = await OperationRepository(session).create(
            workspace_id=workspace_id,
            operation_type=operation_type,
            status=status,
            payload=payload,
        )
        operation.created_at = created_at
        await session.commit()


async def _insert_validation_run(
    engine: AsyncEngine,
    *,
    run_id: str,
    workspace_id: str,
    attempt_id: str,
    target_head_sha: str | None,
    base_sha: str | None = None,
    workspace_head_sha: str | None = None,
    profile_name: str | None = None,
    profile_version: int | None = None,
    profile_source: str | None = None,
    resolved_profile_digest: str | None = None,
    environment_identity_digest: str | None = None,
    environment_identity_inputs: dict[str, object] | None = None,
    tier: int = 1,
    status: str = "succeeded",
    started_at: datetime = datetime(2026, 4, 26, 12, 0, tzinfo=UTC),
    finished_at: datetime = datetime(2026, 4, 26, 12, 5, tzinfo=UTC),
    coverage: dict[str, object] | None = None,
) -> None:
    log_stream_refs: dict[str, object] = {
        "commands": [
            {
                "stdout": "validation.01_validate.stdout",
                "stderr": "validation.01_validate.stderr",
            }
        ]
    }
    if coverage is not None:
        log_stream_refs["coverage"] = coverage

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
                    base_sha,
                    workspace_head_sha,
                    target_branch,
                    target_head_sha,
                    profile_name,
                    profile_version,
                    profile_source,
                    resolved_profile_digest,
                    environment_identity_digest,
                    environment_identity_inputs,
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
                    :tier,
                    :command_set_hash,
                    :commands,
                    'base123',
                    :base_sha,
                    :workspace_head_sha,
                    'codex/merge-queue',
                    :target_head_sha,
                    :profile_name,
                    :profile_version,
                    :profile_source,
                    :resolved_profile_digest,
                    :environment_identity_digest,
                    :environment_identity_inputs,
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
                "base_sha": base_sha,
                "workspace_head_sha": workspace_head_sha,
                "profile_name": profile_name,
                "profile_version": profile_version,
                "profile_source": profile_source,
                "resolved_profile_digest": resolved_profile_digest,
                "environment_identity_digest": environment_identity_digest,
                "environment_identity_inputs": json.dumps(environment_identity_inputs)
                if environment_identity_inputs is not None
                else None,
                "tier": tier,
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
                "started_at": started_at,
                "finished_at": finished_at,
                "log_stream_refs": json.dumps(log_stream_refs),
            },
        )
        await session.commit()


def _candidate_for_reason(**flags: object) -> MergeCandidate:
    candidate = MergeCandidate(
        id="mc_reason",
        task_id="task_reason",
        attempt_id="att_reason",
        workspace_id="ws_reason",
        pr_url="https://github.com/example/reason/pull/1",
        pr_number=1,
        repo_url="git@github.com:example/reason.git",
        base_branch="main",
        branch_name="codex/reason",
        head_sha="head",
        base_sha="base",
        status="open",
    )
    for name, value in flags.items():
        setattr(candidate, name, value)
    return candidate


class TestMergeQueueListPart002:
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
            base_sha="base-identity",
            workspace_head_sha="workspace-head",
            profile_name="python",
            profile_version=4,
            profile_source="repo:.awf/workspace.yml",
            resolved_profile_digest="1" * 64,
            environment_identity_digest="2" * 64,
            environment_identity_inputs={"schema_version": 1},
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
            "base_sha": "base-identity",
            "workspace_head_sha": "workspace-head",
            "target_branch": "codex/merge-queue",
            "target_head_sha": "old-head",
            "current_target_head_sha": "head123",
            "profile_name": "python",
            "profile_version": 4,
            "profile_source": "repo:.awf/workspace.yml",
            "resolved_profile_digest": "1" * 64,
            "environment_identity_digest": "2" * 64,
            "environment_identity_inputs": {"schema_version": 1},
            "identity_source": "persisted",
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
            "freshness_status": "stale",
            "freshness_reason_code": "validation_target_stale",
            "retry_count": 0,
            "coverage_percent": None,
            "coverage_minimum_percent": None,
            "coverage_status": None,
            "coverage_reason_code": None,
            "coverage_gaps": [],
            "failing_test_node_ids": [],
            "failing_test_evidence": [],
        }
        assert item["validation_freshness_status"] == "stale"
        assert item["validation_reason_code"] == "validation_target_stale"
        assert item["required_validation_tier"] == 1
        assert item["latest_satisfied_validation_tier"] == 1

    @pytest.mark.unit
    async def test_exposes_legacy_validation_identity_fallbacks(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
    ) -> None:
        workspace_id = await _create_queue_workspace(
            engine,
            title="Legacy validation provenance",
            status=WorkspaceStatus.monitoring_pr,
            pr_url="https://github.com/example/console/pull/26",
            branch_name="codex/merge-queue",
            updated_at=datetime(2026, 4, 26, 12, 0, tzinfo=UTC),
        )
        attempt_id = await _attempt_id_for_workspace(engine, workspace_id)
        await _insert_validation_run(
            engine,
            run_id="vr_legacy_queue_identity001",
            workspace_id=workspace_id,
            attempt_id=attempt_id,
            target_head_sha="old-head",
        )

        response = await client.get("/v1/merge-queue")

        assert response.status_code == 200
        item = next(
            item for item in response.json()["items"] if item["workspace_id"] == workspace_id
        )
        assert item["latest_validation"]["base_commit"] == "base123"
        assert item["latest_validation"]["base_sha"] == "base123"
        assert item["latest_validation"]["workspace_head_sha"] == "old-head"
        assert item["latest_validation"]["environment_identity_inputs"] == {}
        assert item["latest_validation"]["identity_source"] == "legacy_fallback"
        assert item["latest_validation"]["freshness_status"] == "stale"
        assert item["latest_validation"]["freshness_reason_code"] == "validation_target_stale"
        assert item["required_validation_tier"] == 1
        assert item["latest_satisfied_validation_tier"] == 1

    @pytest.mark.unit
    async def test_merge_queue_freshness_status_is_explicit_for_missing_target(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
    ) -> None:
        workspace_id = await _create_queue_workspace(
            engine,
            title="Missing target identity",
            status=WorkspaceStatus.monitoring_pr,
            pr_url="https://github.com/example/console/pull/127",
            branch_name="codex/merge-queue",
            updated_at=datetime(2026, 4, 26, 12, 0, tzinfo=UTC),
        )
        attempt_id = await _attempt_id_for_workspace(engine, workspace_id)
        await _insert_validation_run(
            engine,
            run_id="vr_missing_target_identity1",
            workspace_id=workspace_id,
            attempt_id=attempt_id,
            target_head_sha=None,
        )

        response = await client.get("/v1/merge-queue")

        assert response.status_code == 200
        item = next(
            item for item in response.json()["items"] if item["workspace_id"] == workspace_id
        )
        assert item["validation_freshness_status"] == "unknown"
        assert item["validation_reason_code"] == "validation_target_unknown"
        assert item["latest_validation"]["fresh_for_target"] is None
        assert item["latest_validation"]["freshness_status"] == "unknown"
        assert item["latest_validation"]["freshness_reason_code"] == "validation_target_unknown"

    @pytest.mark.unit
    async def test_exposes_required_and_latest_satisfied_validation_tiers(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
    ) -> None:
        workspace_id = await _create_queue_workspace(
            engine,
            title="Tier recovery",
            status=WorkspaceStatus.monitoring_pr,
            pr_url="https://github.com/example/console/pull/24",
            branch_name="codex/merge-queue",
            task_class="test_task",
            updated_at=datetime(2026, 4, 26, 12, 8, tzinfo=UTC),
        )
        attempt_id = await _attempt_id_for_workspace(engine, workspace_id)
        await _insert_validation_run(
            engine,
            run_id="vr_240000000000000000000001",
            workspace_id=workspace_id,
            attempt_id=attempt_id,
            target_head_sha="head123",
            tier=1,
            started_at=datetime(2026, 4, 26, 12, 0, tzinfo=UTC),
            finished_at=datetime(2026, 4, 26, 12, 1, tzinfo=UTC),
        )
        await _add_operation(
            engine,
            workspace_id,
            operation_type=OperationType.rebase,
            status=OperationStatus.succeeded,
            created_at=datetime(2026, 4, 26, 12, 2, tzinfo=UTC),
        )
        await _insert_validation_run(
            engine,
            run_id="vr_240000000000000000000002",
            workspace_id=workspace_id,
            attempt_id=attempt_id,
            target_head_sha="head123",
            tier=2,
            started_at=datetime(2026, 4, 26, 12, 3, tzinfo=UTC),
            finished_at=datetime(2026, 4, 26, 12, 4, tzinfo=UTC),
        )
        await _insert_validation_run(
            engine,
            run_id="vr_240000000000000000000003",
            workspace_id=workspace_id,
            attempt_id=attempt_id,
            target_head_sha="head123",
            tier=3,
            status="failed",
            started_at=datetime(2026, 4, 26, 12, 5, tzinfo=UTC),
            finished_at=datetime(2026, 4, 26, 12, 6, tzinfo=UTC),
        )

        response = await client.get("/v1/merge-queue")

        assert response.status_code == 200
        item = next(
            item for item in response.json()["items"] if item["workspace_id"] == workspace_id
        )
        assert item["required_validation_tier"] == 2
        assert item["latest_satisfied_validation_tier"] == 2
        assert item["latest_validation"]["validation_run_id"] == "vr_240000000000000000000003"
        assert item["latest_validation"]["tier"] == 3
        assert item["latest_validation"]["status"] == "failed"

    @pytest.mark.unit
    async def test_latest_satisfied_validation_tier_ignores_run_started_before_rebase(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
    ) -> None:
        workspace_id = await _create_queue_workspace(
            engine,
            title="Overlapping rebase validation",
            status=WorkspaceStatus.monitoring_pr,
            pr_url="https://github.com/example/console/pull/25",
            branch_name="codex/merge-queue",
            task_class="test_task",
            updated_at=datetime(2026, 4, 26, 12, 8, tzinfo=UTC),
        )
        attempt_id = await _attempt_id_for_workspace(engine, workspace_id)
        await _add_operation(
            engine,
            workspace_id,
            operation_type=OperationType.rebase,
            status=OperationStatus.succeeded,
            created_at=datetime(2026, 4, 26, 12, 2, tzinfo=UTC),
        )
        await _insert_validation_run(
            engine,
            run_id="vr_250000000000000000000001",
            workspace_id=workspace_id,
            attempt_id=attempt_id,
            target_head_sha="head123",
            tier=2,
            started_at=datetime(2026, 4, 26, 12, 0, tzinfo=UTC),
            finished_at=datetime(2026, 4, 26, 12, 4, tzinfo=UTC),
        )

        response = await client.get("/v1/merge-queue")

        assert response.status_code == 200
        item = next(
            item for item in response.json()["items"] if item["workspace_id"] == workspace_id
        )
        assert item["required_validation_tier"] == 2
        assert item["latest_satisfied_validation_tier"] is None

    @pytest.mark.unit
    async def test_latest_validation_summary_exposes_coverage_policy(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
    ) -> None:
        workspace_id = await _create_queue_workspace(
            engine,
            title="Coverage provenance",
            status=WorkspaceStatus.monitoring_pr,
            pr_url="https://github.com/example/console/pull/23",
            branch_name="codex/merge-queue",
            updated_at=datetime(2026, 4, 26, 12, 0, tzinfo=UTC),
        )
        attempt_id = await _attempt_id_for_workspace(engine, workspace_id)
        await _insert_validation_run(
            engine,
            run_id="vr_232323232323232323232323",
            workspace_id=workspace_id,
            attempt_id=attempt_id,
            target_head_sha="head123",
            status="failed",
            coverage={
                "provider": "python",
                "percent": 98.4,
                "minimum_percent": 99.0,
                "enforce": True,
                "status": "failed",
                "reason_code": "COVERAGE_BELOW_THRESHOLD",
            },
        )

        response = await client.get("/v1/merge-queue")

        assert response.status_code == 200
        item = next(
            item for item in response.json()["items"] if item["workspace_id"] == workspace_id
        )
        assert item["latest_validation"] is not None
        assert item["latest_validation"]["coverage_percent"] == 98.4
        assert item["latest_validation"]["coverage_minimum_percent"] == 99.0
        assert item["latest_validation"]["coverage_status"] == "failed"
        assert item["latest_validation"]["coverage_reason_code"] == "COVERAGE_BELOW_THRESHOLD"


class TestMergeQueueHelpers:
    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("flags", "expected_reason", "expected_action"),
        [
            ({"completed": True}, "completed", None),
            ({"failed_or_cancelled": True}, "failed_or_cancelled", None),
            ({"not_canonical": True}, "not_canonical", None),
            (
                {"stale": True, "stale_reason": VALIDATION_INSUFFICIENT_TIER_STALE_REASON},
                "stale",
                "validate",
            ),
            ({"stale": True, "stale_reason": "branch_behind_target"}, "stale", "rebase"),
        ],
    )
    def test_candidate_blocker_reason_priority_for_terminal_and_stale_flags(
        self,
        flags: dict[str, object],
        expected_reason: str,
        expected_action: str | None,
    ) -> None:
        candidate = _candidate_for_reason(**flags)

        assert merge_queue_service._merge_blocker_reason(
            candidate,
            stale_reasons=[],
            policy_findings=[],
            queue_blockers=[],
        ) == (expected_reason, expected_action)
