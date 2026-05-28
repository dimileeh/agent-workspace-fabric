"""WorkspaceCleaner tests — compose-down + worktree removal with mocked deps."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, call

import pytest

from awf.node.cleanup import WorkspaceCleaner, WorkspaceCleanupResult, WorkspaceCleanupStepResult
from awf.node.compose_manager import ComposeOperationError
from awf.node.git_manager import GitOperationError


@pytest.fixture
def cleaner() -> tuple[AsyncMock, AsyncMock, WorkspaceCleaner]:
    git = AsyncMock()
    compose = AsyncMock()
    return git, compose, WorkspaceCleaner(git=git, compose=compose)


class TestCleanup:
    @pytest.mark.unit
    def test_cleanup_result_preserves_dataclass_value_equality(self) -> None:
        step = WorkspaceCleanupStepResult(
            name="compose_down",
            status="failed",
            reason_code="COMPOSE_DOWN_FAILED",
            error="network still in use",
        )

        assert WorkspaceCleanupResult(
            status="partial",
            reason_code="CLEANUP_PARTIAL",
            steps=(step,),
        ) == WorkspaceCleanupResult(
            status="partial",
            reason_code="CLEANUP_PARTIAL",
            steps=(step,),
        )

    @pytest.mark.unit
    def test_cleanup_result_preserves_legacy_collection_equality(self) -> None:
        step = WorkspaceCleanupStepResult(
            name="compose_down",
            status="failed",
            reason_code="COMPOSE_DOWN_FAILED",
            error="network still in use",
        )
        result = WorkspaceCleanupResult(
            status="partial",
            reason_code="CLEANUP_PARTIAL",
            steps=(step,),
        )

        assert result == ["compose_down"]
        assert result == ("compose_down",)
        assert result == {"compose_down"}
        assert result != {"worktree_remove"}
        assert result != object()
        assert bool(result) is True
        assert bool(WorkspaceCleanupResult.from_steps([])) is False

    @pytest.mark.unit
    async def test_happy_path_calls_compose_down_then_worktree_remove(
        self, cleaner: tuple[AsyncMock, AsyncMock, WorkspaceCleaner]
    ) -> None:
        git, compose, wc = cleaner

        failures = await wc.cleanup(
            workspace_id="ws_clean",
            repo_url="git@x:y.git",
            worktree_host_path=Path("/tmp/wt"),
        )

        assert failures == []
        compose.down.assert_awaited_once()
        git.remove_worktree.assert_awaited_once_with(
            workspace_id="ws_clean", repo_url="git@x:y.git"
        )

    @pytest.mark.unit
    async def test_cleanup_removes_companion_worktrees_with_primary(
        self, cleaner: tuple[AsyncMock, AsyncMock, WorkspaceCleaner]
    ) -> None:
        git, compose, wc = cleaner

        failures = await wc.cleanup(
            workspace_id="ws_clean",
            repo_url="git@x:app.git",
            companion_worktrees=(
                ("ws_clean__companion__backend", "git@x:backend.git"),
                ("ws_clean__companion__web", "git@x:web.git"),
            ),
        )

        assert failures == []
        compose.down.assert_awaited_once()
        assert git.remove_worktree.await_args_list == [
            call(workspace_id="ws_clean", repo_url="git@x:app.git"),
            call(
                workspace_id="ws_clean__companion__backend",
                repo_url="git@x:backend.git",
            ),
            call(
                workspace_id="ws_clean__companion__web",
                repo_url="git@x:web.git",
            ),
        ]

    @pytest.mark.unit
    async def test_uses_stored_compose_file_path_for_compose_down(
        self,
        cleaner: tuple[AsyncMock, AsyncMock, WorkspaceCleaner],
        tmp_path: Path,
    ) -> None:
        git, compose, wc = cleaner
        compose_file = tmp_path / "compose.yml"
        compose_file.write_text("services: {}\n", encoding="utf-8")

        failures = await wc.cleanup(
            workspace_id="ws_stored",
            repo_url="git@x:y.git",
            compose_project_name="awf_ws_stored",
            compose_file_path=compose_file,
        )

        assert failures == []
        compose.down_project.assert_awaited_once_with(
            project_name="awf_ws_stored",
            compose_file=compose_file,
            workspace_id="ws_stored",
            remove_volumes=True,
        )
        compose.down.assert_not_awaited()
        compose.remove_project_by_label.assert_not_awaited()
        git.remove_worktree.assert_awaited_once()

    @pytest.mark.unit
    async def test_remove_volumes_false_preserves_compose_volumes(
        self, cleaner: tuple[AsyncMock, AsyncMock, WorkspaceCleaner]
    ) -> None:
        git, compose, wc = cleaner

        failures = await wc.cleanup(
            workspace_id="ws_keep_volumes",
            repo_url="git@x:y.git",
            remove_volumes=False,
        )

        assert failures == []
        compose.down.assert_awaited_once()
        assert compose.down.await_args.kwargs["remove_volumes"] is False
        git.remove_worktree.assert_awaited_once()

    @pytest.mark.unit
    async def test_remove_volumes_false_applies_to_stored_compose_file(
        self,
        cleaner: tuple[AsyncMock, AsyncMock, WorkspaceCleaner],
        tmp_path: Path,
    ) -> None:
        git, compose, wc = cleaner
        compose_file = tmp_path / "compose.yml"
        compose_file.write_text("services: {}\n", encoding="utf-8")

        failures = await wc.cleanup(
            workspace_id="ws_keep_volumes",
            repo_url="git@x:y.git",
            compose_file_path=compose_file,
            remove_volumes=False,
        )

        assert failures == []
        compose.down_project.assert_awaited_once_with(
            project_name="awf_ws_keep_volumes",
            compose_file=compose_file,
            workspace_id="ws_keep_volumes",
            remove_volumes=False,
        )
        git.remove_worktree.assert_awaited_once()

    @pytest.mark.unit
    async def test_missing_stored_compose_file_skips_down_project_and_uses_label_cleanup(
        self,
        cleaner: tuple[AsyncMock, AsyncMock, WorkspaceCleaner],
        tmp_path: Path,
    ) -> None:
        git, compose, wc = cleaner
        compose_file = tmp_path / "missing-compose.yml"

        failures = await wc.cleanup(
            workspace_id="ws_missing_compose",
            repo_url="git@x:y.git",
            compose_project_name="awf_ws_missing_compose",
            compose_file_path=compose_file,
            remove_volumes=False,
        )

        assert failures == []
        compose.down_project.assert_not_awaited()
        compose.remove_project_by_label.assert_awaited_once_with(
            project_name="awf_ws_missing_compose",
            workspace_id="ws_missing_compose",
            remove_volumes=False,
        )
        git.remove_worktree.assert_awaited_once()

    @pytest.mark.unit
    async def test_remove_worktree_false_skips_worktree_removal(
        self, cleaner: tuple[AsyncMock, AsyncMock, WorkspaceCleaner]
    ) -> None:
        git, compose, wc = cleaner

        failures = await wc.cleanup(
            workspace_id="ws_keep_worktree",
            repo_url="git@x:y.git",
            remove_worktree=False,
        )

        assert failures == []
        compose.down.assert_awaited_once()
        git.remove_worktree.assert_not_awaited()

    @pytest.mark.unit
    async def test_companion_worktree_skip_records_all_targets_when_remove_worktree_false(
        self, cleaner: tuple[AsyncMock, AsyncMock, WorkspaceCleaner]
    ) -> None:
        git, compose, wc = cleaner

        result = await wc.cleanup(
            workspace_id="ws_keep_worktree",
            repo_url="git@x:app.git",
            companion_worktrees=(
                ("ws_keep_worktree__companion__backend", "git@x:backend.git"),
                ("ws_keep_worktree__companion__web", "git@x:web.git"),
            ),
            remove_worktree=False,
        )

        assert result.status == "succeeded"
        assert [step.to_dict() for step in result.steps] == [
            {
                "name": "compose_down",
                "status": "succeeded",
                "reason_code": "COMPOSE_DOWN_SUCCEEDED",
            },
            {
                "name": "worktree_remove",
                "status": "skipped",
                "reason_code": "WORKTREE_REMOVE_SKIPPED",
            },
            {
                "name": "companion_worktree_remove:ws_keep_worktree__companion__backend",
                "status": "skipped",
                "reason_code": "WORKTREE_REMOVE_SKIPPED",
            },
            {
                "name": "companion_worktree_remove:ws_keep_worktree__companion__web",
                "status": "skipped",
                "reason_code": "WORKTREE_REMOVE_SKIPPED",
            },
        ]
        compose.down.assert_awaited_once()
        git.remove_worktree.assert_not_awaited()

    @pytest.mark.unit
    async def test_compose_failure_does_not_block_worktree_removal(
        self, cleaner: tuple[AsyncMock, AsyncMock, WorkspaceCleaner]
    ) -> None:
        git, compose, wc = cleaner
        compose.down.side_effect = ComposeOperationError(
            operation="down",
            returncode=1,
            stdout="",
            stderr="network still in use",
        )

        failures = await wc.cleanup(
            workspace_id="ws_partial",
            repo_url="git@x:y.git",
        )

        assert "compose_down" in failures
        # Worktree cleanup still ran despite compose error.
        git.remove_worktree.assert_awaited_once()
        assert "worktree_remove" not in failures

    @pytest.mark.unit
    async def test_worktree_failure_captured_but_not_raised(
        self, cleaner: tuple[AsyncMock, AsyncMock, WorkspaceCleaner]
    ) -> None:
        git, compose, wc = cleaner
        git.remove_worktree.side_effect = GitOperationError(
            operation="worktree.remove",
            returncode=1,
            stdout="",
            stderr="worktree locked",
        )

        failures = await wc.cleanup(
            workspace_id="ws_locked",
            repo_url="git@x:y.git",
        )

        assert failures == ["worktree_remove"]
        compose.down.assert_awaited_once()

    @pytest.mark.unit
    async def test_both_failures_both_recorded(
        self, cleaner: tuple[AsyncMock, AsyncMock, WorkspaceCleaner]
    ) -> None:
        git, compose, wc = cleaner
        compose.down.side_effect = ComposeOperationError(
            operation="down", returncode=1, stdout="", stderr="x"
        )
        git.remove_worktree.side_effect = GitOperationError(
            operation="worktree.remove", returncode=1, stdout="", stderr="y"
        )

        failures = await wc.cleanup(
            workspace_id="ws_both_fail",
            repo_url="git@x:y.git",
        )

        assert set(failures) == {"compose_down", "worktree_remove"}
