"""Workspace repository owned-path overlap tests."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import awf.db.repositories as repositories
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import WorkspaceRepository
from tests.postgres import postgres_test_session


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """Yield an isolated PostgreSQL test session."""
    async with postgres_test_session() as s:
        yield s


async def _create_policy_workspace(
    session: AsyncSession,
    repo: WorkspaceRepository,
    *,
    repo_url: str = "git@github.com:example/app.git",
    branch_base: str = "development",
    owned_paths: list[str] | None = None,
    status: WorkspaceStatus = WorkspaceStatus.requested,
    resolved_profile: dict | None = None,
) -> Workspace:
    workspace = await repo.create(
        repo_url=repo_url,
        branch_base=branch_base,
        task_title="policy test",
        task_prompt="do policy-sensitive work",
        agent=AgentRuntime.codex.value,
        test_commands=[],
        owned_paths=list(owned_paths or []),
        resolved_profile=resolved_profile,
    )
    workspace.status = status.value
    await session.flush()
    return workspace


class TestOwnedPathOverlapLookup:
    """Owned-path overlap lookup repository behavior tests."""

    @pytest.mark.unit
    async def test_empty_requested_owned_paths_do_not_report_overlap(
        self, session: AsyncSession
    ) -> None:
        """Verify empty requested owned paths produce no overlap."""
        repo = WorkspaceRepository(session)
        await _create_policy_workspace(session, repo, owned_paths=["src/awf/api/**"])

        overlaps = await repo.find_active_owned_path_overlaps(
            repo_url="git@github.com:example/app.git",
            branch_base="development",
            owned_paths=[],
        )

        assert overlaps == []

    @pytest.mark.unit
    async def test_non_overlapping_owned_paths_do_not_report_overlap(
        self, session: AsyncSession
    ) -> None:
        """Verify non-overlapping requested paths produce no overlap."""
        repo = WorkspaceRepository(session)
        await _create_policy_workspace(session, repo, owned_paths=["src/awf/api/**"])

        overlaps = await repo.find_active_owned_path_overlaps(
            repo_url="git@github.com:example/app.git",
            branch_base="development",
            owned_paths=["docs/**"],
        )

        assert overlaps == []

    @pytest.mark.unit
    async def test_internal_plan_artifact_overlap_does_not_report_interworkspace_overlap(
        self,
        session: AsyncSession,
    ) -> None:
        """Plan-artifact-only matches are excluded from repository overlaps."""
        repo = WorkspaceRepository(session)
        await _create_policy_workspace(
            session,
            repo,
            owned_paths=["src/existing/**", "docs/awf-plans/**"],
        )

        overlaps = await repo.find_active_owned_path_overlaps(
            repo_url="git@github.com:example/app.git",
            branch_base="development",
            owned_paths=["src/requested/**", "docs/awf-plans/**"],
        )

        assert overlaps == []

    @pytest.mark.unit
    async def test_custom_internal_plan_artifact_overlap_does_not_report_interworkspace_overlap(
        self,
        session: AsyncSession,
    ) -> None:
        """Profile-configured planning artifacts are excluded from repository overlaps."""
        custom_profile = {
            "planning": {
                "required": True,
                "plan_path": "docs/alternate/{workspace_id}.md",
                "conformance_report_path": "docs/alternate/{workspace_id}.json",
            },
        }
        existing_artifact_path = "docs/alternate/ws_*.md"
        requested_artifact_path = "docs/alternate/ws_bbbbbbbbbbbbbbbbbbbbbbbb.md"
        assert (
            repositories.owned_path_overlap_match(existing_artifact_path, requested_artifact_path)
            is not None
        )
        repo = WorkspaceRepository(session)
        await _create_policy_workspace(
            session,
            repo,
            owned_paths=[
                "src/existing/**",
                existing_artifact_path,
            ],
            resolved_profile=custom_profile,
        )

        overlaps = await repo.find_active_owned_path_overlaps(
            repo_url="git@github.com:example/app.git",
            branch_base="development",
            owned_paths=[
                "src/requested/**",
                requested_artifact_path,
            ],
            resolved_profile=custom_profile,
        )

        assert overlaps == []

    @pytest.mark.unit
    async def test_custom_profile_unknown_requested_workspace_keeps_real_ws_docs_overlap(
        self,
        session: AsyncSession,
    ) -> None:
        """Requested real docs matching ws_* keep overlap checks before id assignment."""
        custom_profile = {"planning": {"required": True, "plan_path": "docs/{workspace_id}.md"}}
        repo = WorkspaceRepository(session)
        existing = await _create_policy_workspace(
            session,
            repo,
            owned_paths=["docs/ws_protocol.md"],
            resolved_profile=custom_profile,
        )

        overlaps = await repo.find_active_owned_path_overlaps(
            repo_url="git@github.com:example/app.git",
            branch_base="development",
            owned_paths=["docs/ws_protocol.md"],
            resolved_profile=custom_profile,
        )

        assert overlaps == [
            repositories.OwnedPathOverlap(
                workspace_id=existing.id,
                existing_path="docs/ws_protocol.md",
                requested_path="docs/ws_protocol.md",
            )
        ]

    @pytest.mark.unit
    async def test_known_requested_workspace_id_does_not_filter_other_ws_shaped_docs_path(
        self,
        session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Known requested ids keep real ws-shaped docs paths in overlap checks."""
        monkeypatch.setattr(
            repositories,
            "new_workspace_id",
            lambda: "ws_aaaaaaaaaaaaaaaaaaaaaaaa",
        )
        custom_profile = {"planning": {"required": True, "plan_path": "docs/{workspace_id}.md"}}
        repo = WorkspaceRepository(session)
        existing = await _create_policy_workspace(
            session,
            repo,
            owned_paths=["docs/ws_0123456789abcdef01234567.md"],
            resolved_profile=custom_profile,
        )

        overlaps = await repo.find_active_owned_path_overlaps(
            repo_url="git@github.com:example/app.git",
            branch_base="development",
            owned_paths=["docs/ws_0123456789abcdef01234567.md"],
            resolved_profile=custom_profile,
            workspace_id="ws_bbbbbbbbbbbbbbbbbbbbbbbb",
        )

        assert overlaps == [
            repositories.OwnedPathOverlap(
                workspace_id=existing.id,
                existing_path="docs/ws_0123456789abcdef01234567.md",
                requested_path="docs/ws_0123456789abcdef01234567.md",
            )
        ]

    @pytest.mark.unit
    async def test_internal_plan_artifact_filter_does_not_hide_real_overlap(
        self,
        session: AsyncSession,
    ) -> None:
        """Real source overlaps are preserved when plan artifacts also match."""
        repo = WorkspaceRepository(session)
        existing = await _create_policy_workspace(
            session,
            repo,
            owned_paths=["src/shared/**", "docs/awf-plans/**"],
        )

        overlaps = await repo.find_active_owned_path_overlaps(
            repo_url="git@github.com:example/app.git",
            branch_base="development",
            owned_paths=["src/shared/module.py", "docs/awf-plans/**"],
        )

        assert overlaps == [
            repositories.OwnedPathOverlap(
                workspace_id=existing.id,
                existing_path="src/shared/**",
                requested_path="src/shared/module.py",
            )
        ]

    @pytest.mark.unit
    async def test_real_docs_owned_paths_still_report_overlap(
        self,
        session: AsyncSession,
    ) -> None:
        """Repository documentation paths outside AWF internals still overlap."""
        repo = WorkspaceRepository(session)
        existing = await _create_policy_workspace(
            session,
            repo,
            owned_paths=["docs/runbooks/**"],
        )

        overlaps = await repo.find_active_owned_path_overlaps(
            repo_url="git@github.com:example/app.git",
            branch_base="development",
            owned_paths=["docs/runbooks/deploy.md"],
        )

        assert overlaps == [
            repositories.OwnedPathOverlap(
                workspace_id=existing.id,
                existing_path="docs/runbooks/**",
                requested_path="docs/runbooks/deploy.md",
            )
        ]

    @pytest.mark.unit
    async def test_awf_plans_readme_owned_paths_still_report_overlap(
        self,
        session: AsyncSession,
    ) -> None:
        """The tracked awf-plans README is not filtered as generated metadata."""
        repo = WorkspaceRepository(session)
        existing = await _create_policy_workspace(
            session,
            repo,
            owned_paths=["docs/awf-plans/README.md"],
        )

        overlaps = await repo.find_active_owned_path_overlaps(
            repo_url="git@github.com:example/app.git",
            branch_base="development",
            owned_paths=["docs/awf-plans/README.md"],
        )

        assert overlaps == [
            repositories.OwnedPathOverlap(
                workspace_id=existing.id,
                existing_path="docs/awf-plans/README.md",
                requested_path="docs/awf-plans/README.md",
            )
        ]

    @pytest.mark.unit
    async def test_same_paths_on_different_repo_or_base_branch_do_not_report_overlap(
        self, session: AsyncSession
    ) -> None:
        """Verify overlap checks are scoped by repository and base branch."""
        repo = WorkspaceRepository(session)
        await _create_policy_workspace(
            session,
            repo,
            repo_url="git@github.com:example/other.git",
            branch_base="development",
            owned_paths=["src/awf/api/**"],
        )
        await _create_policy_workspace(
            session,
            repo,
            repo_url="git@github.com:example/app.git",
            branch_base="main",
            owned_paths=["src/awf/api/**"],
        )

        overlaps = await repo.find_active_owned_path_overlaps(
            repo_url="git@github.com:example/app.git",
            branch_base="development",
            owned_paths=["src/awf/api/routes/workspaces.py"],
        )

        assert overlaps == []

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "status",
        [
            WorkspaceStatus.completed,
            WorkspaceStatus.failed,
            WorkspaceStatus.cancelled,
            WorkspaceStatus.destroying,
            WorkspaceStatus.destroyed,
        ],
    )
    async def test_terminal_and_teardown_statuses_do_not_report_overlap(
        self,
        session: AsyncSession,
        status: WorkspaceStatus,
    ) -> None:
        """Verify terminal and teardown workspaces do not overlap."""
        repo = WorkspaceRepository(session)
        await _create_policy_workspace(
            session,
            repo,
            owned_paths=["src/awf/api/**"],
            status=status,
        )

        overlaps = await repo.find_active_owned_path_overlaps(
            repo_url="git@github.com:example/app.git",
            branch_base="development",
            owned_paths=["src/awf/api/routes/workspaces.py"],
        )

        assert overlaps == []

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("status", "existing_path", "requested_path"),
        [
            (
                WorkspaceStatus.requested,
                "src/awf/api/routes/workspaces.py",
                "src/awf/api/routes/workspaces.py",
            ),
            (
                WorkspaceStatus.provisioning,
                "src/awf/api",
                "src/awf/api/routes/workspaces.py",
            ),
            (
                WorkspaceStatus.ready,
                "src/awf/api/routes/workspaces.py",
                "src/awf/api",
            ),
            (
                WorkspaceStatus.running,
                "src/awf/api/**",
                "src/awf/api/routes/workspaces.py",
            ),
            (
                WorkspaceStatus.validating,
                "src/awf/api/routes/workspaces.py",
                "src/awf/api/**",
            ),
            (
                WorkspaceStatus.pushing,
                "src/awf/api/*.py",
                "src/awf/api/routes/workspaces.py",
            ),
            (
                WorkspaceStatus.monitoring_pr,
                "src/awf/api/routes/workspaces.py",
                "src/awf/api/*.py",
            ),
            (
                # A blocked workspace keeps its worktree while paused, so its
                # owned paths still occupy and must report overlap.
                WorkspaceStatus.blocked,
                "src/awf/api/**",
                "src/awf/api/routes/workspaces.py",
            ),
            (
                WorkspaceStatus.running,
                "src/awf/api/routes/workspaces.py",
                "src/awf/api/../api/routes/workspaces.py",
            ),
            (
                WorkspaceStatus.validating,
                "src/awf/api/**",
                "src/awf/service/../api/routes/workspaces.py",
            ),
        ],
    )
    async def test_active_exact_ancestor_and_wildcard_paths_report_overlap(
        self,
        session: AsyncSession,
        status: WorkspaceStatus,
        existing_path: str,
        requested_path: str,
    ) -> None:
        """Verify active exact, ancestor, and wildcard paths report overlap."""
        repo = WorkspaceRepository(session)
        existing = await _create_policy_workspace(
            session,
            repo,
            owned_paths=[existing_path],
            status=status,
        )

        overlaps = await repo.find_active_owned_path_overlaps(
            repo_url="git@github.com:example/app.git",
            branch_base="development",
            owned_paths=[requested_path],
        )

        assert len(overlaps) == 1
        assert overlaps[0].workspace_id == existing.id
        assert overlaps[0].existing_path == existing_path
        assert overlaps[0].requested_path == requested_path
