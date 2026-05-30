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
        assert overlap.blocks_merge is True
        assert overlap.severity == "blocking"

    @pytest.mark.unit
    def test_owned_path_target_change_emits_structured_overlap_reason(self) -> None:
        from awf.service.staleness import (
            DEFAULT_STALE_POLICY,
            CandidateSnapshot,
            TargetBranchState,
            evaluate_staleness,
        )

        candidate = CandidateSnapshot(
            owned_paths=("src/awf/service/**",),
            task_class="docs_task",
            base_sha="a" * 40,
        )
        target = TargetBranchState(
            branch="development",
            head_sha="b" * 40,
            changed_paths=("src/awf/service/staleness.py",),
            advanced_commits=1,
        )

        findings = evaluate_staleness(
            candidate=candidate,
            target=target,
            policy=DEFAULT_STALE_POLICY,
        )

        assert len(findings) == 1
        overlap = findings[0]
        assert overlap.reason_code == "STALE_OVERLAP"
        assert overlap.trigger_type == "path_overlap"
        assert overlap.trigger_ref == "src/awf/service/staleness.py"
        assert overlap.severity == "blocking"
        assert overlap.blocks_merge is True
        assert "development" in overlap.explanation

    @pytest.mark.unit
    def test_non_overlapping_target_change_does_not_emit_overlap_reason(self) -> None:
        from awf.service.staleness import (
            DEFAULT_STALE_POLICY,
            CandidateSnapshot,
            TargetBranchState,
            evaluate_staleness,
        )

        candidate = CandidateSnapshot(
            owned_paths=("docs/**",),
            task_class="docs_task",
            base_sha="a" * 40,
        )
        target = TargetBranchState(
            branch="development",
            head_sha="b" * 40,
            changed_paths=("src/awf/service/staleness.py",),
            advanced_commits=1,
        )

        findings = evaluate_staleness(
            candidate=candidate,
            target=target,
            policy=DEFAULT_STALE_POLICY,
        )

        assert findings == []

    @pytest.mark.unit
    def test_plan_artifact_only_overlap_is_advisory_without_target_advanced(self) -> None:
        from awf.service.staleness import (
            DEFAULT_STALE_POLICY,
            CandidateSnapshot,
            TargetBranchState,
            evaluate_staleness,
        )

        candidate = CandidateSnapshot(
            owned_paths=("docs/awf-plans/**",),
            task_class="refactor_task",
            base_sha="a" * 40,
        )
        target = TargetBranchState(
            branch="development",
            head_sha="b" * 40,
            changed_paths=("docs/awf-plans/ws_bbbbbbbbbbbbbbbbbbbbbbbb.conformance.json",),
            advanced_commits=1,
        )

        findings = evaluate_staleness(
            candidate=candidate,
            target=target,
            policy=DEFAULT_STALE_POLICY,
        )

        assert [(f.reason_code, f.blocks_merge, f.severity) for f in findings] == [
            ("ADVISORY_PLAN_ARTIFACT_OVERLAP", False, "advisory")
        ]
        assert findings[0].trigger_type == "plan_artifact_overlap"
        assert (
            findings[0].trigger_ref == "docs/awf-plans/ws_bbbbbbbbbbbbbbbbbbbbbbbb.conformance.json"
        )

    @pytest.mark.unit
    def test_awf_plans_readme_overlap_blocks_as_real_docs_path(self) -> None:
        """The awf-plans README is treated as blocking repository docs."""
        from awf.service.staleness import (
            DEFAULT_STALE_POLICY,
            CandidateSnapshot,
            TargetBranchState,
            evaluate_staleness,
        )

        candidate = CandidateSnapshot(
            owned_paths=("docs/awf-plans/**",),
            task_class="docs_task",
            base_sha="a" * 40,
        )
        target = TargetBranchState(
            branch="development",
            head_sha="b" * 40,
            changed_paths=("docs/awf-plans/README.md",),
            advanced_commits=1,
        )

        findings = evaluate_staleness(
            candidate=candidate,
            target=target,
            policy=DEFAULT_STALE_POLICY,
        )

        assert [(f.reason_code, f.trigger_ref, f.blocks_merge) for f in findings] == [
            ("STALE_OVERLAP", "docs/awf-plans/README.md", True)
        ]

    @pytest.mark.unit
    def test_mixed_plan_artifact_and_source_overlap_blocks_on_source(self) -> None:
        from awf.service.staleness import (
            DEFAULT_STALE_POLICY,
            CandidateSnapshot,
            TargetBranchState,
            evaluate_staleness,
        )

        candidate = CandidateSnapshot(
            owned_paths=("docs/awf-plans/**", "src/awf/service/**"),
            task_class="refactor_task",
            base_sha="a" * 40,
        )
        target = TargetBranchState(
            branch="development",
            head_sha="b" * 40,
            changed_paths=(
                "docs/awf-plans/ws_bbbbbbbbbbbbbbbbbbbbbbbb.md",
                "src/awf/service/staleness.py",
            ),
            advanced_commits=1,
        )

        findings = evaluate_staleness(
            candidate=candidate,
            target=target,
            policy=DEFAULT_STALE_POLICY,
        )

        assert {(f.reason_code, f.trigger_ref, f.blocks_merge, f.severity) for f in findings} == {
            (
                "ADVISORY_PLAN_ARTIFACT_OVERLAP",
                "docs/awf-plans/ws_bbbbbbbbbbbbbbbbbbbbbbbb.md",
                False,
                "advisory",
            ),
            (
                "STALE_OVERLAP",
                "src/awf/service/staleness.py",
                True,
                "blocking",
            ),
        }

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
    def test_migration_task_model_path_change_emits_stale_schema(self) -> None:
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
            changed_paths=("src/app/models/user.py",),
            advanced_commits=1,
        )

        findings = evaluate_staleness(
            candidate=candidate,
            target=target,
            policy=DEFAULT_STALE_POLICY,
        )

        schema = next((f for f in findings if f.reason_code == "STALE_SCHEMA"), None)
        assert schema is not None
        assert schema.trigger_type == "schema_changed"
        assert schema.trigger_ref == "src/app/models/user.py"
        assert "src/app/models/user.py" in schema.explanation
        assert "development" in schema.explanation

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "changed_path",
        (
            "src/app/migrations/versions/20260428_add_user.py",
            "src/app/migration/20260428_add_user.sql",
        ),
    )
    def test_migration_task_nested_migration_path_emits_stale_schema(
        self,
        changed_path: str,
    ) -> None:
        from awf.service.staleness import (
            DEFAULT_STALE_POLICY,
            CandidateSnapshot,
            TargetBranchState,
            evaluate_staleness,
        )

        candidate = CandidateSnapshot(
            owned_paths=("src/app/**",),
            task_class="migration_task",
            base_sha="a" * 40,
        )
        target = TargetBranchState(
            branch="development",
            head_sha="b" * 40,
            changed_paths=(changed_path,),
            advanced_commits=1,
        )

        findings = evaluate_staleness(
            candidate=candidate,
            target=target,
            policy=DEFAULT_STALE_POLICY,
        )

        schema = next((f for f in findings if f.reason_code == "STALE_SCHEMA"), None)
        assert schema is not None
        assert schema.trigger_type == "schema_changed"
        assert schema.trigger_ref == changed_path

    @pytest.mark.unit
    @pytest.mark.parametrize(
        (
            "reason_code",
            "trigger_type",
            "changed_path",
            "dependency_sensitive",
            "build_config_sensitive",
        ),
        [
            (
                "STALE_DEPENDENCY",
                "dependency_changed",
                "uv.lock",
                ("docs_task",),
                (),
            ),
            (
                "STALE_BUILD_CONFIG",
                "build_config_changed",
                ".github/workflows/ci.yml",
                (),
                ("docs_task",),
            ),
        ],
    )
    def test_dependency_and_build_config_sensitivity_is_policy_driven(
        self,
        reason_code: str,
        trigger_type: str,
        changed_path: str,
        dependency_sensitive: tuple[str, ...],
        build_config_sensitive: tuple[str, ...],
    ) -> None:
        from awf.service.staleness import (
            CandidateSnapshot,
            StalePolicy,
            TargetBranchState,
            evaluate_staleness,
        )

        policy = StalePolicy(
            schema_paths=("migrations/",),
            dependency_paths=("uv.lock",),
            build_config_paths=(".github/workflows/",),
            lenient_task_classes=("docs_task", "test_task"),
            dependency_sensitive_task_classes=dependency_sensitive,
            build_config_sensitive_task_classes=build_config_sensitive,
        )
        candidate = CandidateSnapshot(
            owned_paths=("docs/README.md",),
            task_class="docs_task",
            base_sha="a" * 40,
        )
        target = TargetBranchState(
            branch="development",
            head_sha="b" * 40,
            changed_paths=(changed_path,),
            advanced_commits=1,
        )

        findings = evaluate_staleness(candidate=candidate, target=target, policy=policy)

        assert [(f.reason_code, f.trigger_type, f.trigger_ref) for f in findings] == [
            (reason_code, trigger_type, changed_path)
        ]

    @pytest.mark.unit
    def test_docs_and_test_changes_remain_fresh_when_policy_permits(self) -> None:
        from awf.service.staleness import (
            DEFAULT_STALE_POLICY,
            CandidateSnapshot,
            TargetBranchState,
            evaluate_staleness,
        )

        docs_candidate = CandidateSnapshot(
            owned_paths=("docs/getting-started.md",),
            task_class="docs_task",
            base_sha="a" * 40,
        )
        test_candidate = CandidateSnapshot(
            owned_paths=("tests/unit/api/test_health.py",),
            task_class="test_task",
            base_sha="a" * 40,
        )
        target = TargetBranchState(
            branch="development",
            head_sha="b" * 40,
            changed_paths=("docs/reference.md", "tests/unit/service/test_locks.py"),
            advanced_commits=1,
        )

        assert (
            evaluate_staleness(
                candidate=docs_candidate,
                target=target,
                policy=DEFAULT_STALE_POLICY,
            )
            == []
        )
        assert (
            evaluate_staleness(
                candidate=test_candidate,
                target=target,
                policy=DEFAULT_STALE_POLICY,
            )
            == []
        )

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
            ("anything.py", "*", True),
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

    @pytest.mark.unit
    def test_isoformat_handles_none_and_naive_datetimes(self) -> None:
        from awf.service.staleness import _isoformat

        assert _isoformat(None) is None
        assert _isoformat(datetime(2026, 4, 27, 12, 30)) == "2026-04-27T12:30:00+00:00"
