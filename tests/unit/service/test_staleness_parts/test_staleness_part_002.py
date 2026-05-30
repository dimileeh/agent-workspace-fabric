"""Stale detection engine tests.

Covers:
- Pure staleness evaluation logic (``evaluate_staleness``) — owned-path overlap,
  schema/dependency/build-config changes, target-advance defaults.
- ``StalenessRefreshService.refresh_candidate`` — fetching target branch
  state, persisting structured ``StaleReason`` records, marking the
  candidate ``stale`` boolean, and resolving findings that no longer
  apply on the next refresh.
- Event emission so console clients see the structured reason instead of
  having to parse log strings.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.repositories import (
    MergeCandidateRepository,
    TaskAttemptRepository,
    TaskRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from tests.postgres import postgres_test_engine


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


async def _seed_open_candidate(
    factory: async_sessionmaker[AsyncSession],
    *,
    owned_paths: list[str],
    task_class: str | None = None,
    base_sha: str = "a" * 40,
    resolved_profile: dict[str, Any] | None = None,
) -> tuple[str, str, str]:
    """Build a workspace + task + canonical attempt + open merge candidate."""
    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.create(
            repo_url="git@github.com:example/svc.git",
            branch_base="development",
            task_title="Stale fixture",
            task_prompt="Implement.",
            task_external_id="TICKET-STALE",
            agent=AgentRuntime.codex.value,
            test_commands=[],
            owned_paths=owned_paths,
            task_class=task_class,
            resolved_profile=resolved_profile,
        )
        task = await TaskRepository(session).create_or_get(
            repo_url=workspace.repo_url,
            base_branch=workspace.branch_base,
            title=workspace.task_title,
            prompt=workspace.task_prompt,
            external_id=workspace.task_external_id,
            idempotency_key=None,
            task_class=task_class,
            owned_paths=owned_paths,
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
        workspace.branch_name = f"awf/{workspace.id}"
        workspace.remote_push_branch = workspace.branch_name
        workspace.base_commit = base_sha
        workspace.pr_url = "https://github.com/example/svc/pull/41"
        workspace.pr_number = 41
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
        await session.commit()
        return workspace.id, attempt.id, candidate.id


_PAYLOAD_SHAPE_KEYS = {
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
    "detected_at",
    "resolved_at",
    "severity",
    "blocks_merge",
}


class TestStalenessRefreshService:
    """Persistence + lifecycle tests for ``StalenessRefreshService``."""

    @pytest.mark.unit
    async def test_target_advance_creates_active_stale_reason_and_marks_candidate(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from awf.service.staleness import (
            StalenessRefreshService,
            TargetBranchState,
        )

        workspace_id, _attempt_id, candidate_id = await _seed_open_candidate(
            factory,
            owned_paths=["src/awf/api/**"],
            task_class="refactor_task",
        )

        async with factory() as session:
            service = StalenessRefreshService(session)
            await service.refresh_candidate(
                candidate_id,
                target=TargetBranchState(
                    branch="development",
                    head_sha="b" * 40,
                    changed_paths=("src/awf/api/routes/health.py",),
                    advanced_commits=4,
                ),
            )
            await session.commit()

        async with factory() as session:
            from awf.db.repositories import StaleReasonRepository

            repo = StaleReasonRepository(session)
            reasons = await repo.list_active_for_candidate(candidate_id)

            candidate = await MergeCandidateRepository(session).get_by_attempt_id(
                _attempt_id,
            )

        assert candidate is not None
        assert candidate.stale is True
        assert {r.reason_code for r in reasons} >= {"STALE_OVERLAP"}
        first = next(r for r in reasons if r.reason_code == "STALE_OVERLAP")
        assert first.workspace_id == workspace_id
        assert first.candidate_id == candidate_id
        assert first.trigger_type == "path_overlap"
        assert first.status == "active"
        assert first.detected_at is not None
        assert first.resolved_at is None
        assert first.blocks_merge is True
        assert first.severity == "blocking"

    @pytest.mark.unit
    async def test_plan_artifact_only_refresh_records_advisory_without_stale_candidate(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from awf.service.staleness import (
            StalenessRefreshService,
            TargetBranchState,
        )

        _workspace_id, attempt_id, candidate_id = await _seed_open_candidate(
            factory,
            owned_paths=["docs/awf-plans/**"],
            task_class="test_task",
        )

        async with factory() as session:
            service = StalenessRefreshService(session)
            result = await service.refresh_candidate(
                candidate_id,
                target=TargetBranchState(
                    branch="development",
                    head_sha="b" * 40,
                    changed_paths=("docs/awf-plans/ws_bbbbbbbbbbbbbbbbbbbbbbbb.md",),
                    advanced_commits=1,
                ),
            )
            await session.commit()

        async with factory() as session:
            from awf.db.repositories import StaleReasonRepository

            active = await StaleReasonRepository(session).list_active_for_candidate(
                candidate_id,
            )
            candidate = await MergeCandidateRepository(session).get_by_attempt_id(
                attempt_id,
            )

        assert result.stale is False
        assert [(f.reason_code, f.blocks_merge, f.severity) for f in result.findings] == [
            ("ADVISORY_PLAN_ARTIFACT_OVERLAP", False, "advisory")
        ]
        assert candidate is not None
        assert candidate.stale is False
        assert candidate.stale_reason is None
        assert [(r.reason_code, r.blocks_merge, r.severity) for r in active] == [
            ("ADVISORY_PLAN_ARTIFACT_OVERLAP", False, "advisory")
        ]

    @pytest.mark.unit
    async def test_internal_plan_artifact_only_workspace_paths_fall_back_to_attempt_paths(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from awf.service.staleness import (
            StalenessRefreshService,
            TargetBranchState,
        )

        workspace_id, attempt_id, candidate_id = await _seed_open_candidate(
            factory,
            owned_paths=["docs/awf-plans/**"],
            task_class="test_task",
        )

        async with factory() as session:
            attempt = await TaskAttemptRepository(session).get_by_workspace_id(workspace_id)
            assert attempt is not None
            assert attempt.id == attempt_id
            attempt.owned_paths = ["src/shared/**"]
            await session.commit()

        async with factory() as session:
            service = StalenessRefreshService(session)
            result = await service.refresh_candidate(
                candidate_id,
                target=TargetBranchState(
                    branch="development",
                    head_sha="b" * 40,
                    changed_paths=("src/shared/module.py",),
                    advanced_commits=1,
                ),
            )
            await session.commit()

        async with factory() as session:
            from awf.db.repositories import StaleReasonRepository

            active = await StaleReasonRepository(session).list_active_for_candidate(
                candidate_id,
            )
            candidate = await MergeCandidateRepository(session).get_by_attempt_id(
                attempt_id,
            )

        assert result.stale is True
        assert [
            (finding.reason_code, finding.trigger_ref, finding.blocks_merge)
            for finding in result.findings
        ] == [("STALE_OVERLAP", "src/shared/module.py", True)]
        assert candidate is not None
        assert candidate.stale is True
        assert candidate.stale_reason == "STALE_OVERLAP"
        assert [(r.reason_code, r.trigger_ref, r.blocks_merge) for r in active] == [
            ("STALE_OVERLAP", "src/shared/module.py", True)
        ]

    @pytest.mark.unit
    async def test_custom_sibling_plan_artifact_refresh_is_advisory_without_stale_candidate(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from awf.service.staleness import (
            StalenessRefreshService,
            TargetBranchState,
        )

        _workspace_id, attempt_id, candidate_id = await _seed_open_candidate(
            factory,
            owned_paths=["docs/alternate/**"],
            task_class="test_task",
            resolved_profile={
                "planning": {
                    "required": True,
                    "plan_path": "docs/alternate/{workspace_id}.md",
                    "conformance_report_path": "docs/alternate/{workspace_id}.json",
                },
            },
        )

        async with factory() as session:
            service = StalenessRefreshService(session)
            result = await service.refresh_candidate(
                candidate_id,
                target=TargetBranchState(
                    branch="development",
                    head_sha="b" * 40,
                    changed_paths=("docs/alternate/ws_bbbbbbbbbbbbbbbbbbbbbbbb.md",),
                    advanced_commits=1,
                ),
            )
            await session.commit()

        async with factory() as session:
            from awf.db.repositories import StaleReasonRepository

            active = await StaleReasonRepository(session).list_active_for_candidate(
                candidate_id,
            )
            candidate = await MergeCandidateRepository(session).get_by_attempt_id(
                attempt_id,
            )

        assert result.stale is False
        assert [(f.reason_code, f.blocks_merge, f.severity) for f in result.findings] == [
            ("ADVISORY_PLAN_ARTIFACT_OVERLAP", False, "advisory")
        ]
        assert candidate is not None
        assert candidate.stale is False
        assert candidate.stale_reason is None
        assert [(r.reason_code, r.blocks_merge, r.severity) for r in active] == [
            ("ADVISORY_PLAN_ARTIFACT_OVERLAP", False, "advisory")
        ]

    @pytest.mark.unit
    async def test_fetch_target_requires_provider_and_candidate_base_sha(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from awf.service.staleness import StalenessRefreshError, StalenessRefreshService

        _workspace_id, attempt_id, candidate_id = await _seed_open_candidate(
            factory,
            owned_paths=["src/**"],
            task_class="refactor_task",
        )

        async with factory() as session:
            candidate = await MergeCandidateRepository(session).get_by_attempt_id(attempt_id)
            assert candidate is not None
            service = StalenessRefreshService(session)

            with pytest.raises(StalenessRefreshError, match="no provider injected"):
                await service._fetch_target(candidate)

            class _Provider:
                async def fetch(self, **_kwargs: object) -> None:
                    raise AssertionError("base_sha validation should happen before provider fetch")

            candidate.base_sha = None
            service_with_provider = StalenessRefreshService(
                session,
                target_state_provider=_Provider(),  # type: ignore[arg-type]
            )
            with pytest.raises(StalenessRefreshError, match="has no validation base_sha"):
                await service_with_provider._fetch_target(candidate)

    @pytest.mark.unit
    async def test_refresh_preserves_specific_stale_reason_from_readiness(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from awf.service.staleness import (
            StalenessRefreshService,
            TargetBranchState,
        )

        _workspace_id, attempt_id, candidate_id = await _seed_open_candidate(
            factory,
            owned_paths=["src/awf/api/**"],
            task_class="refactor_task",
        )

        async with factory() as session:
            service = StalenessRefreshService(session)
            await service.refresh_candidate(
                candidate_id,
                target=TargetBranchState(
                    branch="development",
                    head_sha="b" * 40,
                    changed_paths=("src/awf/api/routes/health.py",),
                    advanced_commits=4,
                ),
            )
            await session.commit()

        async with factory() as session:
            candidate = await MergeCandidateRepository(session).get_by_attempt_id(
                attempt_id,
            )

        assert candidate is not None
        assert candidate.stale is True
        assert candidate.stale_reason == "validation_insufficient_tier"

    @pytest.mark.unit
    async def test_refresh_preserves_existing_validation_stale_reason(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from awf.service.staleness import (
            StalenessRefreshService,
            TargetBranchState,
        )

        _workspace_id, attempt_id, candidate_id = await _seed_open_candidate(
            factory,
            owned_paths=["src/awf/api/**"],
            task_class="refactor_task",
        )

        async with factory() as session:
            candidate = await MergeCandidateRepository(session).get_by_attempt_id(
                attempt_id,
            )
            assert candidate is not None
            candidate.stale = True
            candidate.stale_reason = "validation_insufficient_tier"
            await session.commit()

        async with factory() as session:
            candidate = await MergeCandidateRepository(session).get_by_attempt_id(
                attempt_id,
            )
            assert candidate is not None
            assert candidate.stale is True
            assert candidate.stale_reason == "validation_insufficient_tier"

        async with factory() as session:
            service = StalenessRefreshService(session)
            await service.refresh_candidate(
                candidate_id,
                target=TargetBranchState(
                    branch="development",
                    head_sha="b" * 40,
                    changed_paths=("src/awf/api/routes/health.py",),
                    advanced_commits=1,
                ),
            )
            await session.commit()

        async with factory() as session:
            from awf.db.repositories import StaleReasonRepository

            candidate = await MergeCandidateRepository(session).get_by_attempt_id(
                attempt_id,
            )
            reasons = await StaleReasonRepository(session).list_active_for_candidate(
                candidate_id,
            )

        assert candidate is not None
        assert candidate.stale is True
        assert candidate.stale_reason == "validation_insufficient_tier"
        assert {r.reason_code for r in reasons} >= {"STALE_OVERLAP"}

    @pytest.mark.unit
    async def test_refresh_replaces_stale_validation_reason_with_active_overlap(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from awf.db.repositories import StaleReasonRepository, ValidationRunRepository
        from awf.service.staleness import (
            StalenessRefreshService,
            TargetBranchState,
        )

        workspace_id, attempt_id, candidate_id = await _seed_open_candidate(
            factory,
            owned_paths=["src/awf/api/**"],
            task_class="refactor_task",
        )

        async with factory() as session:
            validation_repo = ValidationRunRepository(session)
            validation_run = await validation_repo.start(
                workspace_id=workspace_id,
                attempt_id=attempt_id,
                tier=2,
                commands=[],
                base_commit="a" * 40,
                base_sha="a" * 40,
                workspace_head_sha="h" * 40,
                target_branch="development",
                target_head_sha="a" * 40,
                log_stream_refs={},
                started_at=datetime(2026, 4, 29, 12, 0),
            )
            await validation_repo.finish(
                validation_run.id,
                status="succeeded",
                reason_code="VALIDATION_OK",
                finished_at=datetime(2026, 4, 29, 12, 1),
            )
            candidate = await MergeCandidateRepository(session).get_by_attempt_id(
                attempt_id,
            )
            assert candidate is not None
            candidate.stale = True
            candidate.stale_reason = "validation_insufficient_tier"
            await session.commit()

        async with factory() as session:
            service = StalenessRefreshService(session)
            await service.refresh_candidate(
                candidate_id,
                target=TargetBranchState(
                    branch="development",
                    head_sha="b" * 40,
                    changed_paths=("src/awf/api/routes/health.py",),
                    advanced_commits=1,
                ),
            )
            await session.commit()

        async with factory() as session:
            candidate = await MergeCandidateRepository(session).get_by_attempt_id(
                attempt_id,
            )
            reasons = await StaleReasonRepository(session).list_active_for_candidate(
                candidate_id,
            )

        assert candidate is not None
        assert candidate.stale is True
        assert candidate.stale_reason == "STALE_OVERLAP"
        assert [reason.reason_code for reason in reasons] == ["STALE_OVERLAP"]

    @pytest.mark.unit
    async def test_refresh_records_structured_overlap_reason_on_candidate(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from awf.service.staleness import (
            StalenessRefreshService,
            TargetBranchState,
        )

        _workspace_id, attempt_id, candidate_id = await _seed_open_candidate(
            factory,
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

        async with factory() as session:
            candidate = await MergeCandidateRepository(session).get_by_attempt_id(
                attempt_id,
            )

        assert candidate is not None
        assert candidate.stale is True
        assert candidate.stale_reason == "STALE_OVERLAP"

    @pytest.mark.unit
    async def test_refresh_prefers_overlap_as_primary_reason_when_schema_also_matches(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from awf.db.repositories import ValidationRunRepository
        from awf.service.staleness import (
            StalenessRefreshService,
            TargetBranchState,
        )

        workspace_id, attempt_id, candidate_id = await _seed_open_candidate(
            factory,
            owned_paths=["migrations/**"],
            task_class="migration_task",
        )

        async with factory() as session:
            validation_repo = ValidationRunRepository(session)
            validation_run = await validation_repo.start(
                workspace_id=workspace_id,
                attempt_id=attempt_id,
                tier=3,
                commands=[],
                base_commit="a" * 40,
                base_sha="a" * 40,
                workspace_head_sha="h" * 40,
                target_branch="development",
                target_head_sha="a" * 40,
                log_stream_refs={},
                started_at=datetime(2026, 4, 29, 12, 0),
            )
            await validation_repo.finish(
                validation_run.id,
                status="succeeded",
                reason_code="VALIDATION_OK",
                finished_at=datetime(2026, 4, 29, 12, 1),
            )
            await session.commit()

        async with factory() as session:
            service = StalenessRefreshService(session)
            result = await service.refresh_candidate(
                candidate_id,
                target=TargetBranchState(
                    branch="development",
                    head_sha="b" * 40,
                    changed_paths=("migrations/versions/zzz_unrelated.py",),
                    advanced_commits=1,
                ),
            )
            await session.commit()

        async with factory() as session:
            from awf.db.repositories import StaleReasonRepository

            candidate = await MergeCandidateRepository(session).get_by_attempt_id(
                attempt_id,
            )
            reasons = await StaleReasonRepository(session).list_active_for_candidate(
                candidate_id,
            )

        assert {finding.reason_code for finding in result.findings} == {
            "STALE_SCHEMA",
            "STALE_OVERLAP",
        }
        assert {reason.reason_code for reason in reasons} == {
            "STALE_SCHEMA",
            "STALE_OVERLAP",
        }
        assert candidate is not None
        assert candidate.stale is True
        assert candidate.stale_reason == "STALE_OVERLAP"

    @pytest.mark.unit
    async def test_refresh_clears_stale_when_findings_no_longer_apply(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from awf.service.staleness import (
            StalenessRefreshService,
            TargetBranchState,
        )

        _workspace_id, attempt_id, candidate_id = await _seed_open_candidate(
            factory,
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
                    changed_paths=("src/awf/api/routes/health.py",),
                    advanced_commits=2,
                ),
            )
            await session.commit()

        # New target snapshot: rolled back to the original base; no advance.
        async with factory() as session:
            service = StalenessRefreshService(session)
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

        async with factory() as session:
            from awf.db.repositories import StaleReasonRepository

            repo = StaleReasonRepository(session)
            active = await repo.list_active_for_candidate(candidate_id)
            all_reasons = await repo.list_for_candidate(candidate_id)

            candidate = await MergeCandidateRepository(session).get_by_attempt_id(
                attempt_id,
            )

        assert candidate is not None
        assert candidate.stale is False
        assert active == []
        # The historical row remains, marked resolved.
        assert any(r.status == "resolved" for r in all_reasons)
        resolved = next(r for r in all_reasons if r.status == "resolved")
        assert resolved.resolved_at is not None

    @pytest.mark.unit
    async def test_refresh_emits_workspace_event_for_each_finding(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from awf.db.repositories import WorkspaceEventRepository
        from awf.service.staleness import (
            StalenessRefreshService,
            TargetBranchState,
        )

        workspace_id, _attempt_id, candidate_id = await _seed_open_candidate(
            factory,
            owned_paths=["src/awf/api/**"],
            task_class="refactor_task",
        )

        async with factory() as session:
            service = StalenessRefreshService(session)
            await service.refresh_candidate(
                candidate_id,
                target=TargetBranchState(
                    branch="development",
                    head_sha="b" * 40,
                    changed_paths=("src/awf/api/routes/health.py",),
                    advanced_commits=1,
                ),
            )
            await session.commit()

        async with factory() as session:
            events = await WorkspaceEventRepository(session).list(
                workspace_id=workspace_id,
                limit=200,
            )
        stale_events = [evt for evt in events if evt.event_type == "merge_candidate.stale_detected"]
        assert len(stale_events) >= 1
        first = stale_events[0]
        assert first.reason_code == "STALE_OVERLAP"
        assert first.payload is not None
        assert first.payload["candidate_id"] == candidate_id
        assert first.payload["trigger_type"] == "path_overlap"

    @pytest.mark.unit
    async def test_target_state_provider_drives_refresh(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """``StalenessRefreshService`` should accept an injected provider so we
        do not couple core logic to live ``gh`` calls in tests."""
        from awf.service.staleness import (
            StalenessRefreshService,
            TargetBranchState,
            TargetBranchStateProvider,
        )

        _workspace_id, attempt_id, candidate_id = await _seed_open_candidate(
            factory,
            owned_paths=["src/awf/api/**"],
            task_class="refactor_task",
        )

        recorded: list[tuple[str, str, str]] = []

        class _StubProvider(TargetBranchStateProvider):
            async def fetch(
                self,
                *,
                repo_url: str,
                branch: str,
                base_sha: str,
            ) -> TargetBranchState:
                recorded.append((repo_url, branch, base_sha))
                return TargetBranchState(
                    branch=branch,
                    head_sha="z" * 40,
                    changed_paths=("src/awf/api/routes/health.py",),
                    advanced_commits=5,
                )

        async with factory() as session:
            service = StalenessRefreshService(
                session,
                target_state_provider=_StubProvider(),
            )
            result = await service.refresh_candidate(candidate_id)
            await session.commit()

        assert recorded == [("git@github.com:example/svc.git", "development", "a" * 40)]
        async with factory() as session:
            from awf.db.repositories import StaleReasonRepository

            reasons = await StaleReasonRepository(session).list_active_for_candidate(
                candidate_id,
            )
            candidate = await MergeCandidateRepository(session).get_by_attempt_id(
                attempt_id,
            )
        assert candidate is not None
        assert candidate.stale is True
        assert {r.reason_code for r in reasons} >= {"STALE_OVERLAP"}
        assert result.stale is True
        assert {f.reason_code for f in result.findings} >= {"STALE_OVERLAP"}

    @pytest.mark.unit
    @pytest.mark.parametrize(
        (
            "reason_code",
            "trigger_type",
            "changed_path",
            "schema_sensitive",
            "dependency_sensitive",
            "build_config_sensitive",
        ),
        [
            (
                "STALE_SCHEMA",
                "schema_changed",
                "src/app/models/user.py",
                ("docs_task",),
                (),
                (),
            ),
            (
                "STALE_DEPENDENCY",
                "dependency_changed",
                "uv.lock",
                (),
                ("docs_task",),
                (),
            ),
            (
                "STALE_BUILD_CONFIG",
                "build_config_changed",
                "Dockerfile",
                (),
                (),
                ("docs_task",),
            ),
        ],
    )
    async def test_sensitive_reason_refresh_is_idempotent_and_resolves(
        self,
        factory: async_sessionmaker[AsyncSession],
        reason_code: str,
        trigger_type: str,
        changed_path: str,
        schema_sensitive: tuple[str, ...],
        dependency_sensitive: tuple[str, ...],
        build_config_sensitive: tuple[str, ...],
    ) -> None:
        from awf.db.repositories import StaleReasonRepository
        from awf.service.staleness import (
            StalenessRefreshService,
            StalePolicy,
            TargetBranchState,
        )

        _workspace_id, attempt_id, candidate_id = await _seed_open_candidate(
            factory,
            owned_paths=["docs/README.md"],
            task_class="docs_task",
        )
        policy = StalePolicy(
            schema_paths=("src/app/models/",),
            dependency_paths=("uv.lock",),
            build_config_paths=("Dockerfile",),
            lenient_task_classes=("docs_task", "test_task"),
            schema_sensitive_task_classes=schema_sensitive,
            dependency_sensitive_task_classes=dependency_sensitive,
            build_config_sensitive_task_classes=build_config_sensitive,
        )

        async with factory() as session:
            service = StalenessRefreshService(session, policy=policy)
            first = await service.refresh_candidate(
                candidate_id,
                target=TargetBranchState(
                    branch="development",
                    head_sha="b" * 40,
                    changed_paths=(changed_path,),
                    advanced_commits=1,
                ),
            )
            repeated = await service.refresh_candidate(
                candidate_id,
                target=TargetBranchState(
                    branch="development",
                    head_sha="b" * 40,
                    changed_paths=(changed_path,),
                    advanced_commits=1,
                ),
            )
            await session.commit()

        assert [r.reason_code for r in first.newly_added] == [reason_code]
        assert repeated.newly_added == []
        assert repeated.newly_resolved == []

        async with factory() as session:
            repo = StaleReasonRepository(session)
            active = await repo.list_active_for_candidate(candidate_id)
            candidate = await MergeCandidateRepository(session).get_by_attempt_id(
                attempt_id,
            )

        assert candidate is not None
        assert candidate.stale is True
        assert [(r.reason_code, r.trigger_type, r.trigger_ref) for r in active] == [
            (reason_code, trigger_type, changed_path)
        ]

        async with factory() as session:
            service = StalenessRefreshService(session, policy=policy)
            resolved = await service.refresh_candidate(
                candidate_id,
                target=TargetBranchState(
                    branch="development",
                    head_sha="a" * 40,
                    changed_paths=(),
                    advanced_commits=0,
                ),
            )
            await session.commit()

        assert [r.reason_code for r in resolved.newly_resolved] == [reason_code]

        async with factory() as session:
            repo = StaleReasonRepository(session)
            active = await repo.list_active_for_candidate(candidate_id)
            full = await repo.list_for_candidate(candidate_id)
            candidate = await MergeCandidateRepository(session).get_by_attempt_id(
                attempt_id,
            )

        assert candidate is not None
        assert candidate.stale is False
        assert active == []
        historical = next(r for r in full if r.reason_code == reason_code)
        assert historical.status == "resolved"
        assert historical.resolved_at is not None


class TestStalenessRefreshServiceErrorPaths:
    @pytest.mark.unit
    async def test_refresh_unknown_candidate_raises(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        from awf.service.staleness import StalenessRefreshError, StalenessRefreshService

        async with factory() as session:
            service = StalenessRefreshService(session)
            with pytest.raises(StalenessRefreshError):
                await service.refresh_candidate("mc_does_not_exist")

    @pytest.mark.unit
    async def test_refresh_without_target_or_provider_raises(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        from awf.service.staleness import StalenessRefreshError, StalenessRefreshService

        _workspace_id, _attempt_id, candidate_id = await _seed_open_candidate(
            factory,
            owned_paths=["src/awf/api/**"],
            task_class="refactor_task",
        )

        async with factory() as session:
            service = StalenessRefreshService(session)
            with pytest.raises(StalenessRefreshError):
                await service.refresh_candidate(candidate_id)


class TestStaleReasonsRoundTrip:
    """End-to-end TDD-style assertions for the durable ``stale_reasons`` table."""

    @pytest.mark.unit
    async def test_repository_list_excludes_resolved_rows_from_active_view(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from awf.db.repositories import StaleReasonRepository
        from awf.service.staleness import (
            StalenessRefreshService,
            TargetBranchState,
        )

        _workspace_id, _attempt_id, candidate_id = await _seed_open_candidate(
            factory,
            owned_paths=["src/awf/api/**"],
            task_class="refactor_task",
        )

        async with factory() as session:
            service = StalenessRefreshService(session)
            await service.refresh_candidate(
                candidate_id,
                target=TargetBranchState(
                    branch="development",
                    head_sha="b" * 40,
                    changed_paths=("src/awf/api/routes/health.py",),
                    advanced_commits=1,
                ),
            )
            await session.commit()

        async with factory() as session:
            service = StalenessRefreshService(session)
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

        async with factory() as session:
            repo = StaleReasonRepository(session)
            active = await repo.list_active_for_candidate(candidate_id)
            full = await repo.list_for_candidate(candidate_id)

        assert active == []
        assert any(r.status == "resolved" for r in full)


class TestStaleReasonResponseShape:
    @pytest.mark.unit
    def test_default_policy_exposes_known_path_groups(self) -> None:
        from awf.service.staleness import DEFAULT_STALE_POLICY

        assert "migrations/" in tuple(DEFAULT_STALE_POLICY.schema_paths) or any(
            p.startswith("migrations/") for p in DEFAULT_STALE_POLICY.schema_paths
        )
        assert "pyproject.toml" in DEFAULT_STALE_POLICY.dependency_paths
        assert any(
            p.startswith("docker/") or "Dockerfile" in p
            for p in DEFAULT_STALE_POLICY.build_config_paths
        )

    @pytest.mark.unit
    async def test_stale_reason_response_has_required_fields(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from awf.api.schemas import StaleReasonResponse
        from awf.db.repositories import StaleReasonRepository
        from awf.service.staleness import (
            StalenessRefreshService,
            TargetBranchState,
        )

        _workspace_id, _attempt_id, candidate_id = await _seed_open_candidate(
            factory,
            owned_paths=["src/awf/api/**"],
            task_class="refactor_task",
        )

        async with factory() as session:
            service = StalenessRefreshService(session)
            await service.refresh_candidate(
                candidate_id,
                target=TargetBranchState(
                    branch="development",
                    head_sha="b" * 40,
                    changed_paths=("src/awf/api/routes/health.py",),
                    advanced_commits=1,
                ),
            )
            await session.commit()

        async with factory() as session:
            row = (
                await StaleReasonRepository(session).list_active_for_candidate(
                    candidate_id,
                )
            )[0]

        response = StaleReasonResponse.model_validate(row)
        dumped: dict[str, Any] = response.model_dump()
        assert set(dumped.keys()) >= _PAYLOAD_SHAPE_KEYS
        assert isinstance(dumped["detected_at"], (datetime, str))
        assert dumped["severity"] == "blocking"
        assert dumped["blocks_merge"] is True
