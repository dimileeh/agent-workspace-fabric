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

from awf.db.base import Base
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.repositories import (
    MergeCandidateRepository,
    TaskAttemptRepository,
    TaskRepository,
    WorkspaceRepository,
)
from awf.db.session import make_engine, make_session_factory


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield make_session_factory(engine)
    finally:
        await engine.dispose()


async def _seed_open_candidate(
    factory: async_sessionmaker[AsyncSession],
    *,
    owned_paths: list[str],
    task_class: str | None = None,
    base_sha: str = "a" * 40,
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


class TestEvaluateStaleness:
    """Unit tests for the pure ``evaluate_staleness`` function."""

    @pytest.mark.unit
    def test_no_findings_when_target_unchanged(self) -> None:
        from awf.service.staleness import (
            DEFAULT_STALE_POLICY,
            CandidateSnapshot,
            TargetBranchState,
            evaluate_staleness,
        )

        candidate = CandidateSnapshot(
            owned_paths=("src/awf/api/**",),
            task_class=None,
            base_sha="a" * 40,
        )
        target = TargetBranchState(
            branch="development",
            head_sha="a" * 40,
            changed_paths=(),
            advanced_commits=0,
        )

        findings = evaluate_staleness(
            candidate=candidate,
            target=target,
            policy=DEFAULT_STALE_POLICY,
        )

        assert findings == []

    @pytest.mark.unit
    def test_target_advanced_default_invalidates_freshness(self) -> None:
        from awf.service.staleness import (
            DEFAULT_STALE_POLICY,
            CandidateSnapshot,
            TargetBranchState,
            evaluate_staleness,
        )

        candidate = CandidateSnapshot(
            owned_paths=("src/awf/api/**",),
            task_class="refactor_task",
            base_sha="a" * 40,
        )
        target = TargetBranchState(
            branch="development",
            head_sha="b" * 40,
            changed_paths=("docs/UNRELATED.md",),
            advanced_commits=2,
        )

        findings = evaluate_staleness(
            candidate=candidate,
            target=target,
            policy=DEFAULT_STALE_POLICY,
        )

        codes = [f.reason_code for f in findings]
        assert "STALE_TARGET_ADVANCED" in codes

    @pytest.mark.unit
    def test_docs_class_with_non_overlapping_changes_is_not_stale(self) -> None:
        from awf.service.staleness import (
            DEFAULT_STALE_POLICY,
            CandidateSnapshot,
            TargetBranchState,
            evaluate_staleness,
        )

        candidate = CandidateSnapshot(
            owned_paths=("docs/USAGE.md",),
            task_class="docs_task",
            base_sha="a" * 40,
        )
        target = TargetBranchState(
            branch="development",
            head_sha="b" * 40,
            changed_paths=("src/awf/api/routes/health.py",),
            advanced_commits=3,
        )

        findings = evaluate_staleness(
            candidate=candidate,
            target=target,
            policy=DEFAULT_STALE_POLICY,
        )

        assert findings == []

    @pytest.mark.unit
    def test_test_class_with_non_overlapping_changes_is_not_stale(self) -> None:
        from awf.service.staleness import (
            DEFAULT_STALE_POLICY,
            CandidateSnapshot,
            TargetBranchState,
            evaluate_staleness,
        )

        candidate = CandidateSnapshot(
            owned_paths=("tests/unit/api/test_health.py",),
            task_class="test_task",
            base_sha="a" * 40,
        )
        target = TargetBranchState(
            branch="development",
            head_sha="b" * 40,
            changed_paths=("src/awf/api/routes/locks.py",),
            advanced_commits=1,
        )

        findings = evaluate_staleness(
            candidate=candidate,
            target=target,
            policy=DEFAULT_STALE_POLICY,
        )

        assert findings == []

    @pytest.mark.unit
    def test_overlap_emits_stale_overlap_for_any_class(self) -> None:
        from awf.service.staleness import (
            DEFAULT_STALE_POLICY,
            CandidateSnapshot,
            TargetBranchState,
            evaluate_staleness,
        )

        candidate = CandidateSnapshot(
            owned_paths=("src/awf/api/**",),
            task_class="docs_task",
            base_sha="a" * 40,
        )
        target = TargetBranchState(
            branch="development",
            head_sha="b" * 40,
            changed_paths=("src/awf/api/routes/health.py",),
            advanced_commits=1,
        )

        findings = evaluate_staleness(
            candidate=candidate,
            target=target,
            policy=DEFAULT_STALE_POLICY,
        )

        codes = [f.reason_code for f in findings]
        assert "STALE_OVERLAP" in codes
        overlap = next(f for f in findings if f.reason_code == "STALE_OVERLAP")
        assert overlap.trigger_type == "path_overlap"
        assert overlap.trigger_ref == "src/awf/api/routes/health.py"

    @pytest.mark.unit
    def test_migration_task_schema_change_emits_stale_schema(self) -> None:
        from awf.service.staleness import (
            DEFAULT_STALE_POLICY,
            CandidateSnapshot,
            TargetBranchState,
            evaluate_staleness,
        )

        candidate = CandidateSnapshot(
            owned_paths=("migrations/**",),
            task_class="migration_task",
            base_sha="a" * 40,
        )
        target = TargetBranchState(
            branch="development",
            head_sha="b" * 40,
            changed_paths=(
                "migrations/versions/zzz_unrelated.py",
                "src/awf/db/models.py",
            ),
            advanced_commits=2,
        )

        findings = evaluate_staleness(
            candidate=candidate,
            target=target,
            policy=DEFAULT_STALE_POLICY,
        )

        codes = [f.reason_code for f in findings]
        assert "STALE_SCHEMA" in codes

    @pytest.mark.unit
    def test_dependency_task_dependency_change_emits_stale_dependency(self) -> None:
        from awf.service.staleness import (
            DEFAULT_STALE_POLICY,
            CandidateSnapshot,
            TargetBranchState,
            evaluate_staleness,
        )

        candidate = CandidateSnapshot(
            owned_paths=("pyproject.toml",),
            task_class="dependency_task",
            base_sha="a" * 40,
        )
        target = TargetBranchState(
            branch="development",
            head_sha="b" * 40,
            changed_paths=("uv.lock",),
            advanced_commits=1,
        )

        findings = evaluate_staleness(
            candidate=candidate,
            target=target,
            policy=DEFAULT_STALE_POLICY,
        )

        codes = [f.reason_code for f in findings]
        assert "STALE_DEPENDENCY" in codes

    @pytest.mark.unit
    def test_build_config_task_dockerfile_change_emits_stale_build_config(self) -> None:
        from awf.service.staleness import (
            DEFAULT_STALE_POLICY,
            CandidateSnapshot,
            TargetBranchState,
            evaluate_staleness,
        )

        candidate = CandidateSnapshot(
            owned_paths=("docker/**",),
            task_class="build_config_task",
            base_sha="a" * 40,
        )
        target = TargetBranchState(
            branch="development",
            head_sha="b" * 40,
            changed_paths=("docker/Dockerfile.api",),
            advanced_commits=1,
        )

        findings = evaluate_staleness(
            candidate=candidate,
            target=target,
            policy=DEFAULT_STALE_POLICY,
        )

        codes = [f.reason_code for f in findings]
        assert "STALE_BUILD_CONFIG" in codes

    @pytest.mark.unit
    def test_no_findings_when_candidate_has_no_base_sha(self) -> None:
        from awf.service.staleness import (
            DEFAULT_STALE_POLICY,
            CandidateSnapshot,
            TargetBranchState,
            evaluate_staleness,
        )

        candidate = CandidateSnapshot(
            owned_paths=(),
            task_class=None,
            base_sha=None,
        )
        target = TargetBranchState(
            branch="development",
            head_sha="b" * 40,
            changed_paths=("README.md",),
            advanced_commits=2,
        )

        assert (
            evaluate_staleness(
                candidate=candidate,
                target=target,
                policy=DEFAULT_STALE_POLICY,
            )
            == []
        )

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("path", "pattern", "expected"),
        [
            ("a", "", False),
            ("src/awf/api/routes/health.py", "src/awf/api/**", True),
            ("src/awf/api", "src/awf/api/**", True),
            ("src/awf/api/health.py", "src/awf/api/*", True),
            ("src/awf/api/routes/health.py", "src/awf/api/*", False),
            ("docker/Dockerfile", "docker/", True),
            ("docs/USAGE.md", "docs", True),
            ("README.md", "READ", False),
            ("a/b/c.py", "a/?/c.py", True),
        ],
    )
    def test_path_matches_glob_semantics(self, path: str, pattern: str, expected: bool) -> None:
        from awf.service.staleness import _path_matches

        assert _path_matches(path, pattern) is expected

    @pytest.mark.unit
    def test_unknown_class_with_target_advance_uses_target_advanced_default(self) -> None:
        from awf.service.staleness import (
            DEFAULT_STALE_POLICY,
            CandidateSnapshot,
            TargetBranchState,
            evaluate_staleness,
        )

        candidate = CandidateSnapshot(
            owned_paths=(),
            task_class=None,
            base_sha="a" * 40,
        )
        target = TargetBranchState(
            branch="development",
            head_sha="b" * 40,
            changed_paths=("README.md",),
            advanced_commits=1,
        )

        findings = evaluate_staleness(
            candidate=candidate,
            target=target,
            policy=DEFAULT_STALE_POLICY,
        )

        assert [f.reason_code for f in findings] == ["STALE_TARGET_ADVANCED"]


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
}


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
