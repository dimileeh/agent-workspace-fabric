"""Tests for ``scripts.run_awf._materialize_companion`` companion-worktree
ID scoping.

Scope: the Critical fix from CodeRabbit feedback on PR #2 — two
concurrent workspaces that both provision a companion with the same
NAME used to race on a shared ``companion__{name}`` worktree path.
After the fix the ID includes the owning workspace, so they live in
distinct paths.

We fake the ``GitManager`` so the test stays in-memory — we only care
about which ``workspace_id`` the materializer hands it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.node.compose_manager import CompanionService
from scripts.run_awf import _materialize_companion


class _FakeWorktreeLayout:
    """Mirror ``awf.node.git_manager.WorktreeLayout`` enough for the test."""

    def __init__(self, worktree_path: Path, mirror_path: Path) -> None:
        self.worktree_path = worktree_path
        self.mirror_path = mirror_path


class _FakeGitManager:
    """Records every ``remove_worktree`` / ``add_worktree`` call so the
    test can assert on the ``workspace_id`` parameter."""

    def __init__(self, tmp_path: Path) -> None:
        self.removed_ids: list[str] = []
        self.added_ids: list[str] = []
        self.added_new_branches: list[str] = []
        self._tmp = tmp_path

    async def remove_worktree(self, *, workspace_id: str, repo_url: str) -> None:
        self.removed_ids.append(workspace_id)

    async def add_worktree(
        self,
        *,
        workspace_id: str,
        repo_url: str,
        base_branch: str,
        new_branch: str,
    ) -> _FakeWorktreeLayout:
        self.added_ids.append(workspace_id)
        self.added_new_branches.append(new_branch)
        # Return paths that are at least plausible — the test doesn't
        # read them, but the real return type has these attributes.
        return _FakeWorktreeLayout(
            worktree_path=self._tmp / workspace_id,
            mirror_path=self._tmp / "mirrors" / workspace_id,
        )


_COMPANION_RAW = {
    "name": "backend",
    "repo_url": "git@github.com:dimileeh/aira-agent.git",
    "branch": "development",
}


class TestCompanionWorktreeScoping:
    @pytest.mark.unit
    async def test_worktree_id_includes_owner_workspace(self, tmp_path: Path) -> None:
        """The Critical bug: ``companion__{name}`` was unscoped. Now
        the ID must carry the owning workspace so concurrent materializers
        never collide."""
        git = _FakeGitManager(tmp_path)

        await _materialize_companion(
            dict(_COMPANION_RAW),
            git=git,  # type: ignore[arg-type]
            owner_workspace_id="ws_aaaaaaaa",
        )

        assert git.added_ids == ["companion__ws_aaaaaaaa__backend"]
        assert git.removed_ids == ["companion__ws_aaaaaaaa__backend"]

    @pytest.mark.unit
    async def test_two_workspaces_same_companion_get_distinct_paths(self, tmp_path: Path) -> None:
        """Concurrent-run regression: two workspaces with same-named
        companions must land in different worktree IDs — this is the
        whole point of the fix."""
        git1 = _FakeGitManager(tmp_path)
        git2 = _FakeGitManager(tmp_path)

        await _materialize_companion(
            dict(_COMPANION_RAW), git=git1, owner_workspace_id="ws_aaaaaaaa"
        )
        await _materialize_companion(
            dict(_COMPANION_RAW), git=git2, owner_workspace_id="ws_bbbbbbbb"
        )

        # Different workspace → different companion IDs → no collision.
        assert git1.added_ids[0] == "companion__ws_aaaaaaaa__backend"
        assert git2.added_ids[0] == "companion__ws_bbbbbbbb__backend"
        assert git1.added_ids[0] != git2.added_ids[0]

    @pytest.mark.unit
    async def test_new_branch_name_also_scoped(self, tmp_path: Path) -> None:
        """The per-companion local branch (``awf-companion/<something>``)
        is created fresh each run. Previously used ``os.getpid()`` as a
        uniqueness suffix, which only worked per-process and still
        collided under ``asyncio.gather`` in the same process. Now uses
        the owner workspace id — same scoping as the worktree ID."""
        git = _FakeGitManager(tmp_path)

        await _materialize_companion(
            dict(_COMPANION_RAW),
            git=git,
            owner_workspace_id="ws_cafebabe",
        )

        assert git.added_new_branches == ["awf-companion/ws_cafebabe-backend"]

    @pytest.mark.unit
    async def test_local_build_context_still_supported(self, tmp_path: Path) -> None:
        """Companions without ``repo_url`` use an on-disk build context
        and don't touch git. Regression guard: the workspace-scoping
        refactor must not break this path."""
        git = _FakeGitManager(tmp_path)
        build_ctx = tmp_path / "local-ctx"
        build_ctx.mkdir()

        svc = await _materialize_companion(
            {"name": "local", "build_context": str(build_ctx)},
            git=git,
            owner_workspace_id="ws_abc",
        )

        assert isinstance(svc, CompanionService)
        assert svc.name == "local"
        # No git operations happened — no repo_url.
        assert git.added_ids == []
        assert git.removed_ids == []

    @pytest.mark.unit
    async def test_env_file_expansion_runs_on_companions(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Companion loader still expands tilde/envvar on env_file
        (from the earlier fix). Regression guard: the workspace-scoping
        change shares the same function; don't break the env-file path."""
        git = _FakeGitManager(tmp_path)
        monkeypatch.setenv("AWF_TEST_AIRA_ROOT", str(tmp_path))

        svc = await _materialize_companion(
            {
                "name": "local",
                "build_context": str(tmp_path),
                "env_file": "${AWF_TEST_AIRA_ROOT}/.env",
            },
            git=git,
            owner_workspace_id="ws_ignored_here",
        )

        assert svc.env_file == str(tmp_path / ".env")
