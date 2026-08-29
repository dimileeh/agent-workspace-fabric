"""GitManager tests against a real throwaway git repository.

We intentionally exercise the real ``git`` CLI rather than mock it — the whole
value of GitManager is correct handling of real worktree semantics, so mocking
would test nothing useful. Setup is fast (git init + two commits < 200 ms on
typical hardware) and each test gets an isolated ``tmp_path``.
"""

from __future__ import annotations

import asyncio
import os
import signal
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import awf.node.git_manager as git_manager
from awf.node import git_manager as git_module
from awf.node.git_manager import (
    GitManager,
    GitOperationError,
    _agent_writable_git_targets,
)
from awf.runtime.worktree_writer_lock import (
    exclusive_worktree_writer_lock,
    worktree_writer_lock_path,
)


def _git(args: list[str], cwd: Path) -> None:
    """Run a synchronous git command for fixture setup; fail loudly on error."""
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def origin_repo(tmp_path: Path) -> Path:
    """Create a minimal git repository with two commits on ``development``.

    Acts as the "origin" URL the GitManager will clone as a mirror.
    """
    repo = tmp_path / "origin"
    repo.mkdir()
    _git(["init", "-q", "-b", "development"], repo)
    _git(["config", "user.name", "AWF Test"], repo)
    _git(["config", "user.email", "awf@test.local"], repo)

    (repo / "README.md").write_text("first\n")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "init"], repo)

    (repo / "README.md").write_text("second\n")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "update"], repo)
    return repo


@pytest.fixture
def work_dir(tmp_path: Path) -> Path:
    d = tmp_path / "awf-work"
    d.mkdir()
    return d


@pytest.fixture
def manager(work_dir: Path) -> GitManager:
    return GitManager(work_dir)


class TestEnsureMirror:
    @pytest.mark.unit
    async def test_clones_on_first_call(self, manager: GitManager, origin_repo: Path) -> None:
        mirror = await manager.ensure_mirror(str(origin_repo))
        assert mirror.exists()
        assert (mirror / "HEAD").exists()  # bare repo has HEAD at the top level

    @pytest.mark.unit
    async def test_updates_on_second_call(
        self, manager: GitManager, origin_repo: Path, tmp_path: Path
    ) -> None:
        mirror = await manager.ensure_mirror(str(origin_repo))
        first_call_path = mirror

        # Add a new commit on the origin.
        (origin_repo / "NEW.md").write_text("added later\n")
        _git(["add", "."], origin_repo)
        _git(["commit", "-q", "-m", "third"], origin_repo)

        mirror_again = await manager.ensure_mirror(str(origin_repo))
        assert mirror_again == first_call_path  # same path, no re-clone

        # The fetched commit is reachable from origin/development. Local
        # heads are deleted post-clone so they don't drift — base-branch
        # lookups go through the remote-tracking ref.
        rev_list = subprocess.run(
            [
                "git",
                "--git-dir",
                str(mirror_again),
                "rev-list",
                "--count",
                "origin/development",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        assert int(rev_list.stdout.strip()) == 3

    @pytest.mark.unit
    async def test_concurrent_first_calls_for_different_repos_do_not_collide(
        self, manager: GitManager, origin_repo: Path, tmp_path: Path
    ) -> None:
        other = tmp_path / "origin-2"
        other.mkdir()
        _git(["init", "-q", "-b", "main"], other)
        _git(["config", "user.name", "x"], other)
        _git(["config", "user.email", "x@x"], other)
        (other / "f").write_text("a\n")
        _git(["add", "."], other)
        _git(["commit", "-q", "-m", "a"], other)

        m1, m2 = await asyncio.gather(
            manager.ensure_mirror(str(origin_repo)),
            manager.ensure_mirror(str(other)),
        )
        assert m1 != m2
        assert m1.exists() and m2.exists()


class TestEnsureMirrorBitbucketAuth:
    """Bitbucket git-auth preflight in ``ensure_mirror`` (issue #461).

    The preflight converts an otherwise opaque clone failure (or TTY hang) for a
    private bitbucket.org repo into a fast, reason-coded error, and leaves the
    GitHub path completely unchanged.
    """

    _BB_URL = "https://bitbucket.org/ws/repo.git"
    _TOKEN = "ATATT-mirror-token-do-not-render"

    @pytest.mark.unit
    async def test_missing_credentials_raise_before_any_git_runs(
        self,
        manager: GitManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("BITBUCKET_API_TOKEN", raising=False)
        monkeypatch.delenv("BITBUCKET_EMAIL", raising=False)

        calls: list[list[str]] = []

        async def _record(args: list[str], *, operation: str) -> git_module.GitResult:
            calls.append(args)
            return git_module.GitResult(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(manager, "_run", _record)

        with pytest.raises(GitOperationError) as raised:
            await manager.ensure_mirror(self._BB_URL)

        assert raised.value.reason_code == "BITBUCKET_GIT_AUTH_NOT_CONFIGURED"
        # No mirror exists yet, so the failure is labelled as a clone.
        assert raised.value.operation == "mirror.clone"
        # No git subprocess should have run: we fail fast, never attempting an
        # unauthenticated clone of a private repo.
        assert calls == []
        # The error names the missing var, never a secret value.
        assert self._TOKEN not in str(raised.value)

    @pytest.mark.unit
    async def test_missing_credentials_on_existing_mirror_label_update(
        self,
        manager: GitManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # When the mirror already exists, ``ensure_mirror`` would only fetch, so a
        # credential failure must be labelled ``mirror.update``, not ``mirror.clone``.
        monkeypatch.delenv("BITBUCKET_API_TOKEN", raising=False)
        monkeypatch.delenv("BITBUCKET_EMAIL", raising=False)

        manager._mirrors_dir.mkdir(parents=True, exist_ok=True)
        manager._mirror_path(self._BB_URL).mkdir(parents=True, exist_ok=True)

        with pytest.raises(GitOperationError) as raised:
            await manager.ensure_mirror(self._BB_URL)

        assert raised.value.reason_code == "BITBUCKET_GIT_AUTH_NOT_CONFIGURED"
        assert raised.value.operation == "mirror.update"

    @pytest.mark.unit
    async def test_configured_credentials_clone_with_plain_url(
        self,
        work_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("BITBUCKET_API_TOKEN", raising=False)
        monkeypatch.delenv("BITBUCKET_EMAIL", raising=False)
        manager = GitManager(
            work_dir,
            env={
                "BITBUCKET_API_TOKEN": self._TOKEN,
                "BITBUCKET_EMAIL": "agent@example.com",
            },
        )

        calls: list[list[str]] = []

        async def _record(args: list[str], *, operation: str) -> git_module.GitResult:
            calls.append(args)
            return git_module.GitResult(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(manager, "_run", _record)

        await manager.ensure_mirror(self._BB_URL)

        # Preflight passed and the clone ran with the URL unchanged — the token
        # is never embedded in the clone URL (auth flows via the env helper).
        clone_calls = [args for args in calls if args[:3] == ["git", "clone", "--mirror"]]
        assert clone_calls, "expected a git clone --mirror invocation"
        assert self._BB_URL in clone_calls[0]
        # The token must not appear in any git argument.
        assert all(self._TOKEN not in arg for call in calls for arg in call)

    @pytest.mark.unit
    async def test_github_repo_skips_bitbucket_preflight(
        self,
        manager: GitManager,
        origin_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A non-bitbucket repo must clone normally with no bitbucket env at all.
        monkeypatch.delenv("BITBUCKET_API_TOKEN", raising=False)
        monkeypatch.delenv("BITBUCKET_EMAIL", raising=False)

        mirror = await manager.ensure_mirror(str(origin_repo))

        assert mirror.exists()
        assert (mirror / "HEAD").exists()


class TestAddWorktree:
    @pytest.mark.unit
    async def test_creates_worktree_with_new_branch(
        self, manager: GitManager, origin_repo: Path
    ) -> None:
        layout = await manager.add_worktree(
            workspace_id="ws_abc123",
            repo_url=str(origin_repo),
            base_branch="development",
            new_branch="awf/ws_abc123",
        )

        assert layout.worktree_path.exists()
        assert (layout.worktree_path / "README.md").read_text() == "second\n"

        branch = subprocess.run(
            ["git", "-C", str(layout.worktree_path), "symbolic-ref", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert branch == "awf/ws_abc123"

    @pytest.mark.unit
    async def test_creates_worktree_from_github_pull_head_ref(
        self, manager: GitManager, origin_repo: Path
    ) -> None:
        _git(["switch", "-q", "-c", "fork-pr-head"], origin_repo)
        (origin_repo / "FORK.md").write_text("from pull head\n")
        _git(["add", "FORK.md"], origin_repo)
        _git(["commit", "-q", "-m", "fork pr head"], origin_repo)
        pr_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=origin_repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        _git(["update-ref", "refs/pull/42/head", pr_head], origin_repo)
        _git(["switch", "-q", "development"], origin_repo)
        _git(["branch", "-D", "fork-pr-head"], origin_repo)

        layout = await manager.add_worktree(
            workspace_id="ws_pr_head",
            repo_url=str(origin_repo),
            base_branch="refs/pull/42/head",
            new_branch="feature-sync/ws_pr_head",
        )

        assert (layout.worktree_path / "FORK.md").read_text() == "from pull head\n"
        head = subprocess.run(
            ["git", "-C", str(layout.worktree_path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        tracking = subprocess.run(
            [
                "git",
                "--git-dir",
                str(layout.mirror_path),
                "rev-parse",
                "refs/remotes/origin/pull/42/head",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert head == pr_head
        assert tracking == pr_head

    @pytest.mark.unit
    async def test_rejects_duplicate_worktree_path(
        self, manager: GitManager, origin_repo: Path
    ) -> None:
        await manager.add_worktree(
            workspace_id="ws_dup",
            repo_url=str(origin_repo),
            base_branch="development",
            new_branch="awf/ws_dup",
        )
        with pytest.raises(GitOperationError) as exc:
            await manager.add_worktree(
                workspace_id="ws_dup",
                repo_url=str(origin_repo),
                base_branch="development",
                new_branch="awf/ws_dup-2",
            )
        assert exc.value.reason_code == "GIT_WORKTREE_ALREADY_EXISTS"

    @pytest.mark.unit
    async def test_rejects_missing_base_branch(
        self, manager: GitManager, origin_repo: Path
    ) -> None:
        with pytest.raises(GitOperationError) as exc:
            await manager.add_worktree(
                workspace_id="ws_missing",
                repo_url=str(origin_repo),
                base_branch="does-not-exist",
                new_branch="awf/ws_missing",
            )
        assert exc.value.reason_code == "GIT_BASE_BRANCH_MISSING"

    @pytest.mark.unit
    async def test_prepares_linked_worktree_git_paths_for_agent_user(
        self, origin_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        chowned: list[tuple[Path, int, int]] = []
        monkeypatch.setattr(os, "geteuid", lambda: 0)
        monkeypatch.setattr(
            os,
            "chown",
            lambda path, uid, gid: chowned.append((Path(path), uid, gid)),
        )
        manager = GitManager(
            tmp_path / "awf-work",
            worktree_owner_uid=1000,
            worktree_owner_gid=1000,
        )

        layout = await manager.add_worktree(
            workspace_id="ws_agent_owner",
            repo_url=str(origin_repo),
            base_branch="development",
            new_branch="awf/ws_agent_owner",
        )

        assert (layout.worktree_path, 1000, 1000) in chowned
        assert (layout.mirror_path, 1000, 1000) in chowned
        assert (layout.mirror_path / "hooks", 1000, 1000) in chowned
        assert (layout.mirror_path / "objects", 1000, 1000) in chowned
        assert (layout.mirror_path / "refs", 1000, 1000) in chowned
        assert (layout.mirror_path / "worktrees", 1000, 1000) in chowned
        assert (
            layout.mirror_path / "worktrees" / "ws_agent_owner",
            1000,
            1000,
        ) in chowned
        assert (layout.worktree_path / "README.md", 1000, 1000) in chowned
        object_files = [
            path for path in (layout.mirror_path / "objects").glob("*/*") if path.is_file()
        ]
        assert object_files
        assert all((path, 1000, 1000) not in chowned for path in object_files)
        hook_files = [path for path in (layout.mirror_path / "hooks").glob("*") if path.is_file()]
        assert hook_files
        assert any((path, 1000, 1000) in chowned for path in hook_files)

    @pytest.mark.unit
    async def test_agent_owner_repair_skips_unwritable_loose_object_files(
        self, origin_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager_without_owner = GitManager(tmp_path / "awf-work")
        mirror = await manager_without_owner.ensure_mirror(str(origin_repo))
        protected_object = next(path for path in (mirror / "objects").glob("*/*") if path.is_file())

        chowned: list[Path] = []

        def fake_chown(
            path: str | bytes | os.PathLike[str] | os.PathLike[bytes], uid: int, gid: int
        ) -> None:
            target = Path(path)
            if target == protected_object:
                raise PermissionError(target)
            chowned.append(target)

        monkeypatch.setattr(os, "geteuid", lambda: 0)
        monkeypatch.setattr(os, "chown", fake_chown)
        manager = GitManager(
            tmp_path / "awf-work",
            worktree_owner_uid=1000,
            worktree_owner_gid=1000,
        )

        layout = await manager.add_worktree(
            workspace_id="ws_object_skip",
            repo_url=str(origin_repo),
            base_branch="development",
            new_branch="awf/ws_object_skip",
        )

        assert layout.worktree_path in chowned
        assert mirror / "objects" in chowned
        assert protected_object not in chowned

    @pytest.mark.unit
    def test_repair_agent_writable_worktree_makes_object_dirs_owner_writable(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mirror = tmp_path / "mirror.git"
        objects = mirror / "objects"
        fanout = objects / "ab"
        fanout.mkdir(parents=True)
        loose_object = fanout / "cdef"
        loose_object.write_text("object\n", encoding="utf-8")
        pack = objects / "pack"
        pack.mkdir()
        worktree = tmp_path / "worktree"
        linked_git_dir = mirror / "worktrees" / "ws"
        linked_git_dir.mkdir(parents=True)
        worktree.mkdir()
        (worktree / ".git").write_text(f"gitdir: {linked_git_dir}\n", encoding="utf-8")
        for directory in (objects, fanout, pack):
            directory.chmod(0o500)
        loose_object.chmod(0o400)

        monkeypatch.setattr(os, "geteuid", lambda: 0)
        monkeypatch.setattr(os, "chown", lambda *_args: None)
        monkeypatch.setattr(os, "lchown", lambda *_args: None)

        git_manager.repair_agent_writable_worktree(mirror, worktree)

        for directory in (objects, fanout, pack):
            assert directory.stat().st_mode & stat.S_IWUSR
        assert not loose_object.stat().st_mode & stat.S_IWUSR

    @pytest.mark.unit
    async def test_agent_owner_preparation_skips_when_owner_or_root_is_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fail_chown(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("chown should not run")

        monkeypatch.setattr(git_manager, "_chown_targets", fail_chown)
        without_owner = GitManager(tmp_path / "no-owner")
        await without_owner._prepare_agent_writable_worktree(  # noqa: SLF001
            layout_mirror=tmp_path / "mirror.git",
            worktree_path=tmp_path / "worktree",
        )

        monkeypatch.setattr(os, "geteuid", lambda: 1000)
        non_root = GitManager(
            tmp_path / "non-root",
            worktree_owner_uid=1000,
            worktree_owner_gid=1000,
        )
        await non_root._prepare_agent_writable_worktree(  # noqa: SLF001
            layout_mirror=tmp_path / "mirror.git",
            worktree_path=tmp_path / "worktree",
        )


@pytest.mark.unit
class TestAgentWorktreeWritable:
    """Regression coverage for the local UID/GID strategy.

    Locks the contract that an agent-runtime user can run ``git status``,
    ``git add``, and ``git commit`` inside a prepared worktree, and that the
    helper that drives post-provision ownership repair lists every
    mirror-bare directory required for a downstream commit while skipping
    loose object files (the ``aa866959`` fix).
    """

    @pytest.mark.unit
    async def test_agent_writable_targets_lists_required_paths_excluding_loose_objects(
        self, manager: GitManager, origin_repo: Path
    ) -> None:
        layout = await manager.add_worktree(
            workspace_id="ws_targets",
            repo_url=str(origin_repo),
            base_branch="development",
            new_branch="awf/ws_targets",
        )
        # ``git clone --mirror`` does not create top-level ``logs/`` (bare
        # repos default to ``core.logAllRefUpdates=false``). Operators who
        # enable ref-log on the mirror end up with a ``logs/`` dir; the
        # helper must include it so the agent can append to it. Force the
        # dir to exist for this contract test.
        (layout.mirror_path / "logs").mkdir(exist_ok=True)

        targets = _agent_writable_git_targets(
            layout_mirror=layout.mirror_path,
            worktree_path=layout.worktree_path,
        )
        target_paths = {t.path for t in targets}
        recursive_paths = {t.path for t in targets if t.recursive}
        directories_only_paths = {t.path for t in targets if t.directories_only}

        # Mirror-bare directories git needs to write into when the agent
        # commits in the worktree (HEAD update, ref log, lock files).
        assert layout.worktree_path in target_paths
        assert layout.mirror_path in target_paths
        assert layout.mirror_path / "hooks" in target_paths
        assert layout.mirror_path / "refs" in target_paths
        assert layout.mirror_path / "logs" in target_paths
        assert layout.mirror_path / "worktrees" in target_paths
        assert layout.mirror_path / "objects" in target_paths
        assert layout.mirror_path / "hooks" in recursive_paths

        # Loose object files must not be chowned recursively. Docker Desktop
        # on macOS rejects the chown when host metadata is missing, which
        # caused the regression fixed in commit aa866959.
        objects_target = next(t for t in targets if t.path == layout.mirror_path / "objects")
        assert objects_target.recursive
        assert objects_target.directories_only
        assert layout.mirror_path / "objects" in directories_only_paths

        # The mirror itself and the worktrees admin dir are non-recursive
        # (only the directory entry is repaired) — recursive chown there
        # would walk over loose objects again.
        assert layout.mirror_path not in recursive_paths
        assert layout.mirror_path / "worktrees" not in recursive_paths

    @pytest.mark.unit
    def test_agent_writable_targets_omits_logs_when_mirror_lacks_it(self, tmp_path: Path) -> None:
        """Bare mirrors default to no top-level logs/ — the helper must not
        synthesize a target for a non-existent path or the chown step would
        attempt to chown a missing entry on macOS Docker Desktop."""
        mirror = tmp_path / "mirror.git"
        worktree = tmp_path / "wt"
        mirror.mkdir()
        worktree.mkdir()
        (mirror / "objects").mkdir()
        (mirror / "refs").mkdir()
        (mirror / "worktrees").mkdir()

        targets = _agent_writable_git_targets(layout_mirror=mirror, worktree_path=worktree)
        target_paths = {t.path for t in targets}

        assert mirror / "logs" not in target_paths
        assert mirror / "refs" in target_paths
        assert mirror / "objects" in target_paths
        assert mirror / "worktrees" in target_paths

    @pytest.mark.unit
    def test_agent_writable_targets_skips_linked_git_dir_when_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mirror = tmp_path / "mirror.git"
        worktree = tmp_path / "wt"
        mirror.mkdir()
        worktree.mkdir()
        monkeypatch.setattr(git_manager, "linked_worktree_git_dir", lambda _worktree: None)

        targets = _agent_writable_git_targets(layout_mirror=mirror, worktree_path=worktree)

        assert targets == (
            git_manager._ChownTarget(worktree, recursive=True),  # noqa: SLF001
            git_manager._ChownTarget(mirror, recursive=False),  # noqa: SLF001
        )

    @pytest.mark.unit
    def test_agent_writable_targets_uses_explicit_linked_git_dir(self, tmp_path: Path) -> None:
        mirror = tmp_path / "mirror.git"
        worktree = tmp_path / "wt"
        linked_git_dir = tmp_path / "linked.git"
        mirror.mkdir()
        worktree.mkdir()
        linked_git_dir.mkdir()

        targets = _agent_writable_git_targets(
            layout_mirror=mirror,
            worktree_path=worktree,
            linked_git_dir=linked_git_dir,
        )

        assert targets == (
            git_manager._ChownTarget(worktree, recursive=True),  # noqa: SLF001
            git_manager._ChownTarget(linked_git_dir, recursive=True),  # noqa: SLF001
            git_manager._ChownTarget(mirror, recursive=False),  # noqa: SLF001
        )

    @pytest.mark.unit
    async def test_prepared_worktree_supports_agent_git_status_add_commit(
        self, manager: GitManager, origin_repo: Path, tmp_path: Path
    ) -> None:
        """End-to-end: the prepared worktree accepts the three required git commands.

        Exercises the realistic local mode where the controlling process is
        the same UID as the prepared worktree (agent UID 1000 on Linux CI).
        Privileged simulation of the root control-plane is covered by
        ``test_prepares_linked_worktree_git_paths_for_agent_user``; this
        test verifies the layout is actually usable.
        """
        layout = await manager.add_worktree(
            workspace_id="ws_agent_git",
            repo_url=str(origin_repo),
            base_branch="development",
            new_branch="awf/ws_agent_git",
        )

        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "AWF Test",
            "GIT_AUTHOR_EMAIL": "awf@test.local",
            "GIT_COMMITTER_NAME": "AWF Test",
            "GIT_COMMITTER_EMAIL": "awf@test.local",
        }

        status = subprocess.run(
            ["git", "-C", str(layout.worktree_path), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
        assert status.stdout == ""

        sentinel = layout.worktree_path / "AGENT_WROTE_THIS.md"
        sentinel.write_text("agent-side commit smoke\n")

        subprocess.run(
            ["git", "-C", str(layout.worktree_path), "add", sentinel.name],
            check=True,
            capture_output=True,
            env=env,
        )

        subprocess.run(
            [
                "git",
                "-C",
                str(layout.worktree_path),
                "commit",
                "-m",
                "agent commit",
            ],
            check=True,
            capture_output=True,
            env=env,
        )

        # The new commit is reachable from the worktree's branch and the
        # bare mirror's ref log was updated (proves ``logs/`` and ``refs/``
        # were writable end-to-end).
        head = subprocess.run(
            ["git", "-C", str(layout.worktree_path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        mirror_branch_sha = subprocess.run(
            [
                "git",
                "--git-dir",
                str(layout.mirror_path),
                "rev-parse",
                f"refs/heads/{layout.branch_name}",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert head == mirror_branch_sha


class TestRemoveWorktree:
    """Tests for RemoveWorktree."""

    @pytest.mark.unit
    async def test_removes_existing_worktree(self, manager: GitManager, origin_repo: Path) -> None:
        """Verify removes existing worktree."""
        layout = await manager.add_worktree(
            workspace_id="ws_rm",
            repo_url=str(origin_repo),
            base_branch="development",
            new_branch="awf/ws_rm",
        )
        assert layout.worktree_path.exists()
        with exclusive_worktree_writer_lock(layout.worktree_path):
            pass
        assert worktree_writer_lock_path(layout.worktree_path).exists()

        await manager.remove_worktree(workspace_id="ws_rm", repo_url=str(origin_repo))
        assert not layout.worktree_path.exists()
        assert not worktree_writer_lock_path(layout.worktree_path).exists()

    @pytest.mark.unit
    async def test_remove_worktree_waits_for_writer_lock(
        self, manager: GitManager, origin_repo: Path
    ) -> None:
        """Teardown must not delete the checkout while a writer holds the lock."""
        layout = await manager.add_worktree(
            workspace_id="ws_rm_writer_lock",
            repo_url=str(origin_repo),
            base_branch="development",
            new_branch="awf/ws_rm_writer_lock",
        )
        entered: list[str] = []
        real_run = manager._run

        async def _tracking_run(args: list[str], *, operation: str):  # type: ignore[no-untyped-def]
            entered.append(operation)
            return await real_run(args, operation=operation)

        manager._run = _tracking_run  # type: ignore[method-assign]

        lock_held = threading.Event()
        release_lock = threading.Event()

        def _hold_writer_lock() -> None:
            with exclusive_worktree_writer_lock(layout.worktree_path):
                lock_held.set()
                release_lock.wait(timeout=5.0)

        holder = threading.Thread(target=_hold_writer_lock, name="writer-lock-holder")
        holder.start()
        assert lock_held.wait(timeout=5.0)

        task = asyncio.create_task(
            manager.remove_worktree(
                workspace_id="ws_rm_writer_lock",
                repo_url=str(origin_repo),
            )
        )
        await asyncio.sleep(0)
        assert entered == []
        assert layout.worktree_path.exists()
        assert task.done() is False

        release_lock.set()
        await task
        holder.join(timeout=5.0)
        assert holder.is_alive() is False
        assert entered == ["worktree.remove", "worktree.prune"]
        assert not layout.worktree_path.exists()

    @pytest.mark.unit
    async def test_missing_worktree_is_noop(self, manager: GitManager, origin_repo: Path) -> None:
        # Ensure mirror exists so only worktree is "missing."
        await manager.ensure_mirror(str(origin_repo))
        # Should not raise.
        await manager.remove_worktree(workspace_id="ws_never_created", repo_url=str(origin_repo))

    @pytest.mark.unit
    async def test_stale_metadata_worktree_remove_is_success(
        self, manager: GitManager, origin_repo: Path
    ) -> None:
        """Verify stale metadata worktree remove is success."""
        # A directory exists at the worktree path but was never registered as a
        # git worktree, so ``git worktree remove`` emits
        # ``fatal: '<path>' is not a working tree``. Removal must be idempotent.
        await manager.ensure_mirror(str(origin_repo))
        worktree_path = manager._worktrees_dir / "ws_stale"
        worktree_path.mkdir(parents=True)
        (worktree_path / "leftover.txt").write_text("stale\n")
        assert worktree_path.exists()

        pruned: list[str] = []
        real_run = manager._run

        async def _tracking_run(args: list[str], *, operation: str):  # type: ignore[no-untyped-def]
            if operation == "worktree.prune":
                pruned.append(operation)
            return await real_run(args, operation=operation)

        manager._run = _tracking_run  # type: ignore[method-assign]

        # Idempotent success: the already-removed condition is not an error.
        await manager.remove_worktree(workspace_id="ws_stale", repo_url=str(origin_repo))
        # ``git worktree prune`` still ran to clear stale metadata.
        assert pruned == ["worktree.prune"]
        # The leftover directory and its contents must be physically reclaimed —
        # ``git worktree remove`` never ran, so GC would otherwise silently
        # retain the disk space it reports as freed.
        assert not worktree_path.exists()

    @pytest.mark.unit
    async def test_missing_gitfile_worktree_validation_failure_is_reclaimed(
        self, manager: GitManager, origin_repo: Path
    ) -> None:
        """Verify missing gitfile worktree validation failure is reclaimed."""
        await manager.ensure_mirror(str(origin_repo))
        worktree_path = manager._worktrees_dir / "ws_missing_gitfile"
        worktree_path.mkdir(parents=True)
        (worktree_path / "leftover.txt").write_text("stale\n")

        pruned: list[str] = []

        async def _stale_run(args: list[str], *, operation: str):  # type: ignore[no-untyped-def]
            """Test helper for stale run."""
            if operation == "worktree.remove":
                raise GitOperationError(
                    operation=operation,
                    returncode=128,
                    stdout="",
                    stderr=(
                        "fatal: validation failed, cannot remove working tree: "
                        f"'{worktree_path}/.git' does not exist"
                    ),
                )
            if operation == "worktree.prune":
                pruned.append(operation)
                return subprocess.CompletedProcess(args, 0, "", "")
            raise AssertionError(f"unexpected operation {operation}")

        manager._run = _stale_run  # type: ignore[method-assign]

        await manager.remove_worktree(
            workspace_id="ws_missing_gitfile",
            repo_url=str(origin_repo),
        )

        assert pruned == ["worktree.prune"]
        assert not worktree_path.exists()

    @pytest.mark.unit
    async def test_standalone_git_dir_at_managed_path_is_not_reclaimed(
        self, manager: GitManager, origin_repo: Path
    ) -> None:
        """Standalone ``.git`` directory must fail closed, not be erased.

        A plain clone at a managed path is not proven AWF-linked leftover metadata;
        reclaiming it would destroy unrelated commits or uncommitted work.
        """
        await manager.ensure_mirror(str(origin_repo))
        worktree_path = manager._worktrees_dir / "ws_standalone_gitdir"
        worktree_path.mkdir(parents=True)
        (worktree_path / ".git").mkdir()
        (worktree_path / "leftover.txt").write_text("orphan\n")

        async def _unexpected_run(args: list[str], *, operation: str):  # type: ignore[no-untyped-def]
            raise AssertionError(
                f"git must not run when refusing a standalone repo; got {operation}"
            )

        manager._run = _unexpected_run  # type: ignore[method-assign]

        with pytest.raises(GitOperationError) as excinfo:
            await manager.remove_worktree(
                workspace_id="ws_standalone_gitdir",
                repo_url=str(origin_repo),
            )

        assert excinfo.value.reason_code == "GIT_WORKTREE_STANDALONE_REPO"
        assert worktree_path.exists()
        assert (worktree_path / ".git").is_dir()
        assert (worktree_path / "leftover.txt").read_text() == "orphan\n"

    @pytest.mark.unit
    async def test_stale_dir_reclaim_failure_propagates(
        self, manager: GitManager, origin_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify stale dir reclaim failure propagates."""
        # ``git worktree remove`` reports the path is not a working tree, so the
        # rmtree fallback is responsible for reclaiming the directory. If that
        # deletion genuinely fails (e.g. a permission error), the failure must
        # surface as a ``GitOperationError`` rather than being swallowed — else
        # callers record removal success while the directory leaks on disk.
        await manager.ensure_mirror(str(origin_repo))
        worktree_path = manager._worktrees_dir / "ws_rmfail"
        worktree_path.mkdir(parents=True)
        (worktree_path / "leftover.txt").write_text("stale\n")

        async def _stale_run(args: list[str], *, operation: str):  # type: ignore[no-untyped-def]
            """Test helper for stale run."""
            if operation == "worktree.remove":
                raise GitOperationError(
                    operation=operation,
                    returncode=1,
                    stdout="",
                    stderr=f"fatal: '{worktree_path}' is not a working tree",
                )
            raise AssertionError(f"unexpected operation {operation}")

        manager._run = _stale_run  # type: ignore[method-assign]

        def _boom(path: Path) -> None:
            raise PermissionError(f"cannot remove {path}")

        monkeypatch.setattr("awf.node.git_manager.shutil.rmtree", _boom)

        with pytest.raises(GitOperationError) as excinfo:
            await manager.remove_worktree(workspace_id="ws_rmfail", repo_url=str(origin_repo))
        assert excinfo.value.reason_code == "GIT_WORKTREE_REMOVE_FAILED"
        # The directory is still on disk; the failure was reported, not hidden.
        assert worktree_path.exists()

    @pytest.mark.unit
    async def test_genuine_remove_error_still_raises(
        self, manager: GitManager, origin_repo: Path
    ) -> None:
        await manager.ensure_mirror(str(origin_repo))
        worktree_path = manager._worktrees_dir / "ws_boom"
        worktree_path.mkdir(parents=True)

        async def _failing_run(args: list[str], *, operation: str):  # type: ignore[no-untyped-def]
            if operation == "worktree.remove":
                raise GitOperationError(
                    operation=operation,
                    returncode=1,
                    stdout="",
                    stderr="fatal: some other failure",
                )
            raise AssertionError(f"unexpected operation {operation}")

        manager._run = _failing_run  # type: ignore[method-assign]

        with pytest.raises(GitOperationError):
            await manager.remove_worktree(workspace_id="ws_boom", repo_url=str(origin_repo))


_BAD_IDS = [
    "..",  # contains ".."
    "ws_../../etc",  # ".." + "/"
    "ws_a..b",  # embedded ".." without a slash (dot/hyphen class must still reject)
    "ws_x__companion__..",  # companion name ".." must not reach the sink
    "ws_abc/def",  # contains "/"
    "ws_abc\\def",  # contains "\" (a separator on Windows)
    "ws_a\n..b",  # ".." hidden behind a newline from the lookahead's ".*"
    "ws_abc\x00",  # null byte
    ".hidden",  # leading "."
    "",  # empty string
]
_BAD_ID_LABELS = [
    "dotdot",
    "dotdot-slash",
    "embedded-dotdot",
    "companion-dotdot",
    "slash",
    "backslash",
    "newline-dotdot",
    "null-byte",
    "leading-dot",
    "empty",
]


class TestWorkspaceIdValidation:
    """Defense-in-depth validation of ``workspace_id`` at the worktree-path sink.

    Every method that joins a caller-supplied ``workspace_id`` onto the managed
    worktree root routes through ``GitManager._worktree_path_for``. A traversal-
    prone id (``..``, ``/``, leading ``.``, null byte, empty) must raise
    ``GitOperationError`` *before* any git subprocess runs.
    """

    @staticmethod
    def _install_run_recorder(
        manager: GitManager, monkeypatch: pytest.MonkeyPatch
    ) -> list[list[str]]:
        """Patch ``manager._run`` with a recorder that never shells out to git.

        Every git call in the guarded methods (including ``ensure_mirror``)
        funnels through ``self._run``, so an empty recorder proves no git
        subprocess ran.
        """
        calls: list[list[str]] = []

        async def _record(args: list[str], *, operation: str) -> git_module.GitResult:
            calls.append(args)
            return git_module.GitResult(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(manager, "_run", _record)
        return calls

    @pytest.mark.unit
    @pytest.mark.parametrize("bad_id", _BAD_IDS, ids=_BAD_ID_LABELS)
    def test_worktree_path_for_rejects_traversal_ids(
        self, manager: GitManager, bad_id: str
    ) -> None:
        with pytest.raises(GitOperationError) as raised:
            manager._worktree_path_for(bad_id)  # noqa: SLF001

        assert raised.value.reason_code == "GIT_WORKSPACE_ID_INVALID"
        assert raised.value.operation == "worktree.path"
        # The rejected value is surfaced clearly (repr keeps null bytes/newlines
        # visible) and never resolved into a traversal path that looks valid.
        assert "invalid workspace id" in str(raised.value).lower()

    @pytest.mark.unit
    @pytest.mark.parametrize("bad_id", _BAD_IDS, ids=_BAD_ID_LABELS)
    async def test_add_worktree_rejects_bad_id_without_running_git(
        self, manager: GitManager, monkeypatch: pytest.MonkeyPatch, bad_id: str
    ) -> None:
        calls = self._install_run_recorder(manager, monkeypatch)

        with pytest.raises(GitOperationError) as raised:
            await manager.add_worktree(
                workspace_id=bad_id,
                repo_url="https://github.com/awf/repo.git",
                base_branch="development",
                new_branch="awf/x",
            )

        assert raised.value.reason_code == "GIT_WORKSPACE_ID_INVALID"
        # Validation precedes ``ensure_mirror``'s clone/fetch: no git ran.
        assert calls == []

    @pytest.mark.unit
    @pytest.mark.parametrize("bad_id", _BAD_IDS, ids=_BAD_ID_LABELS)
    async def test_head_sha_rejects_bad_id_without_running_git(
        self, manager: GitManager, monkeypatch: pytest.MonkeyPatch, bad_id: str
    ) -> None:
        calls = self._install_run_recorder(manager, monkeypatch)

        with pytest.raises(GitOperationError) as raised:
            await manager.head_sha(workspace_id=bad_id)

        assert raised.value.reason_code == "GIT_WORKSPACE_ID_INVALID"
        assert calls == []

    @pytest.mark.unit
    @pytest.mark.parametrize("bad_id", _BAD_IDS, ids=_BAD_ID_LABELS)
    async def test_remove_worktree_rejects_bad_id_without_running_git(
        self,
        manager: GitManager,
        origin_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
        bad_id: str,
    ) -> None:
        calls = self._install_run_recorder(manager, monkeypatch)

        with pytest.raises(GitOperationError) as raised:
            await manager.remove_worktree(workspace_id=bad_id, repo_url=str(origin_repo))

        assert raised.value.reason_code == "GIT_WORKSPACE_ID_INVALID"
        # Covers the flagged ``worktree remove --force`` sink via
        # ``remove_worktree_from_mirror``: no git ran.
        assert calls == []

    @pytest.mark.unit
    async def test_remove_worktree_rejects_symlink_escaping_worktrees_root(
        self, manager: GitManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A valid workspace ID must not make cleanup follow an external symlink."""
        external_worktree = tmp_path / "external-worktree"
        external_worktree.mkdir()
        manager._worktrees_dir.mkdir()  # noqa: SLF001
        (manager._worktrees_dir / "ws_escape").symlink_to(  # noqa: SLF001
            external_worktree,
            target_is_directory=True,
        )
        calls = self._install_run_recorder(manager, monkeypatch)

        with pytest.raises(GitOperationError) as raised:
            await manager.remove_worktree_from_mirror(
                workspace_id="ws_escape",
                mirror_path=tmp_path / "mirror.git",
            )

        assert raised.value.reason_code == "GIT_WORKTREE_PATH_ESCAPE"
        assert calls == []
        assert external_worktree.exists()

    @pytest.mark.unit
    @pytest.mark.parametrize("bad_id", _BAD_IDS, ids=_BAD_ID_LABELS)
    def test_get_worktree_path_rejects_bad_id(self, manager: GitManager, bad_id: str) -> None:
        with pytest.raises(GitOperationError) as raised:
            manager.get_worktree_path(bad_id)

        assert raised.value.reason_code == "GIT_WORKSPACE_ID_INVALID"

    @pytest.mark.unit
    def test_worktree_path_for_accepts_realistic_id(self, manager: GitManager) -> None:
        workspace_id = "ws_" + "0123456789abcdef01234567"  # ws_ + 24 hex
        assert (
            manager._worktree_path_for(workspace_id)  # noqa: SLF001
            == manager._worktrees_dir / workspace_id
        )

    @pytest.mark.unit
    def test_worktree_path_for_accepts_companion_style_id(self, manager: GitManager) -> None:
        # Companion / isolated-reask worktree ids use only underscore markers,
        # so the loose pattern keeps them compatible.
        workspace_id = "ws_deadbeef__companion__redis"
        assert (
            manager._worktree_path_for(workspace_id)  # noqa: SLF001
            == manager._worktrees_dir / workspace_id
        )

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "companion_name",
        ["api.v2", "api-v2", "redis-cache", "aira.web", "a.b-c_d"],
        ids=["dot", "hyphen", "hyphen-word", "dotted", "mixed"],
    )
    def test_worktree_path_for_accepts_companion_name_with_dot_or_hyphen(
        self, manager: GitManager, companion_name: str
    ) -> None:
        # ``ServiceName`` (api/schemas_companions.py) permits ``.`` and ``-`` in
        # companion service names, so ``{workspace_id}__companion__{name}`` ids
        # can legitimately contain them. The sink must accept these or companion
        # provision / head_sha / cleanup wrongly raise GIT_WORKSPACE_ID_INVALID.
        workspace_id = f"ws_deadbeef__companion__{companion_name}"
        assert (
            manager._worktree_path_for(workspace_id)  # noqa: SLF001
            == manager._worktrees_dir / workspace_id
        )

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "workspace_id",
        ["ws_legacy:name", "ws_legacy name", "ws_legacy@host", "ws_legacy+1", "ws_"],
        ids=["colon", "space", "at-sign", "plus", "bare-prefix"],
    )
    def test_worktree_path_for_accepts_any_safe_single_path_component(
        self, manager: GitManager, workspace_id: str
    ) -> None:
        # Orphan GC classifies EVERY on-disk directory whose name starts with
        # ``ws_`` (service/orphans.py and service/orphan_resources.py), so a
        # legacy/synthetic name using characters outside the currently generated
        # id grammar still reaches this sink. Rejecting it here made the reap fail
        # permanently with GIT_WORKSPACE_ID_INVALID instead of falling back to
        # filesystem deletion; anything that cannot escape the worktree root must
        # be accepted.
        assert (
            manager._worktree_path_for(workspace_id)  # noqa: SLF001
            == manager._worktrees_dir / workspace_id
        )


class TestHeadSha:
    """Tests for resolving the current HEAD SHA from a worktree."""

    @pytest.mark.unit
    async def test_returns_current_head(self, manager: GitManager, origin_repo: Path) -> None:
        """Verify returns current head."""
        layout = await manager.add_worktree(
            workspace_id="ws_head",
            repo_url=str(origin_repo),
            base_branch="development",
            new_branch="awf/ws_head",
        )
        sha = await manager.head_sha(workspace_id="ws_head")
        assert len(sha) == 40
        assert all(c in "0123456789abcdef" for c in sha)

        expected = subprocess.run(
            ["git", "-C", str(layout.worktree_path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert sha == expected


class TestAncestorBaseProbes:
    """Ancestry helpers used when retaining a PR base after checkout."""

    @pytest.mark.unit
    async def test_is_ancestor_and_merge_base_for_linear_and_divergent_histories(
        self,
        manager: GitManager,
        origin_repo: Path,
        tmp_path: Path,
    ) -> None:
        layout = await manager.add_worktree(
            workspace_id="ws_anc",
            repo_url=str(origin_repo),
            base_branch="development",
            new_branch="awf/ws_anc",
        )
        base = await manager.head_sha(workspace_id="ws_anc")
        (layout.worktree_path / "feature.txt").write_text("feature\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(layout.worktree_path), "add", "feature.txt"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(layout.worktree_path),
                "-c",
                "user.name=T",
                "-c",
                "user.email=t@t",
                "commit",
                "-q",
                "-m",
                "feature",
            ],
            check=True,
            capture_output=True,
        )
        head = await manager.head_sha(workspace_id="ws_anc")

        assert await manager.is_ancestor_of_head(workspace_id="ws_anc", commit=base) is True
        assert await manager.is_ancestor_of_head(workspace_id="ws_anc", commit=head) is True
        assert await manager.merge_base_with_head(workspace_id="ws_anc", commit=base) == base

        # Simulate an advanced target tip that is not an ancestor of HEAD: create
        # a sibling commit on a detached tip that shares the original base.
        sibling = tmp_path / "sibling"
        subprocess.run(
            ["git", "clone", "--quiet", str(origin_repo), str(sibling)],
            check=True,
            capture_output=True,
        )
        (sibling / "advance.txt").write_text("advance\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(sibling), "add", "advance.txt"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(sibling),
                "-c",
                "user.name=T",
                "-c",
                "user.email=t@t",
                "commit",
                "-q",
                "-m",
                "advance",
            ],
            check=True,
            capture_output=True,
        )
        advanced = subprocess.run(
            ["git", "-C", str(sibling), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        # Fetch the advanced tip into the worktree object store without checking it out.
        subprocess.run(
            ["git", "-C", str(layout.worktree_path), "fetch", str(sibling), "HEAD:refs/temp/adv"],
            check=True,
            capture_output=True,
        )

        assert await manager.is_ancestor_of_head(workspace_id="ws_anc", commit=advanced) is False
        assert await manager.merge_base_with_head(workspace_id="ws_anc", commit=advanced) == base
        assert await manager.merge_base_with_head(workspace_id="ws_anc", commit="0" * 40) is None


async def _wait_for_pid_file(path: Path) -> int:
    for _ in range(200):
        if path.is_file():
            return int(path.read_text(encoding="utf-8"))
        await asyncio.sleep(0.01)
    raise AssertionError(f"child pid file not written: {path}")


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@pytest.mark.unit
async def test_git_manager_run_terminates_subprocess_on_cancellation(
    manager: GitManager,
    tmp_path: Path,
) -> None:
    pid_file = tmp_path / "child.pid"
    task = asyncio.create_task(
        manager._run(  # noqa: SLF001
            [
                sys.executable,
                "-c",
                (
                    "import os,pathlib,sys,time;"
                    "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()));"
                    "time.sleep(30)"
                ),
                str(pid_file),
            ],
            operation="test.cancel",
        )
    )
    pid = await _wait_for_pid_file(pid_file)

    try:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert not _pid_exists(pid)
    finally:
        if _pid_exists(pid):
            os.kill(pid, signal.SIGKILL)


class TestEnsureWorktree:
    """Idempotent worktree restore used by hosted monitor restart recovery."""

    @pytest.mark.unit
    async def test_ensure_worktree_no_op_when_checkout_valid(
        self, manager: GitManager, origin_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        layout = await manager.add_worktree(
            workspace_id="ws_ensure_valid",
            repo_url=str(origin_repo),
            base_branch="development",
            new_branch="awf/ws_ensure_valid",
        )
        add_calls: list[str] = []

        async def _add(**kwargs: object) -> object:
            add_calls.append("add")
            raise AssertionError("add_worktree must not run")

        monkeypatch.setattr(manager, "add_worktree", _add)
        again = await manager.ensure_worktree(
            workspace_id="ws_ensure_valid",
            repo_url=str(origin_repo),
            base_branch="development",
            new_branch="awf/ws_ensure_valid",
        )
        assert again.worktree_path == layout.worktree_path
        assert add_calls == []

    @pytest.mark.unit
    async def test_ensure_worktree_recreates_missing_checkout(
        self, manager: GitManager, origin_repo: Path
    ) -> None:
        layout = await manager.add_worktree(
            workspace_id="ws_ensure_missing",
            repo_url=str(origin_repo),
            base_branch="development",
            new_branch="awf/ws_ensure_missing",
        )
        import shutil

        shutil.rmtree(layout.worktree_path)
        assert not layout.worktree_path.exists()

        restored = await manager.ensure_worktree(
            workspace_id="ws_ensure_missing",
            repo_url=str(origin_repo),
            base_branch="development",
            new_branch="awf/ws_ensure_missing",
        )
        assert restored.worktree_path.is_dir()
        assert (restored.worktree_path / ".git").exists()
        sha = await manager.head_sha(workspace_id="ws_ensure_missing")
        assert len(sha) == 40

    @pytest.mark.unit
    async def test_ensure_worktree_prunes_stale_metadata_then_recreates(
        self, manager: GitManager, origin_repo: Path
    ) -> None:
        layout = await manager.add_worktree(
            workspace_id="ws_ensure_stale",
            repo_url=str(origin_repo),
            base_branch="development",
            new_branch="awf/ws_ensure_stale",
        )
        import shutil

        # Delete the directory but leave mirror worktree admin metadata.
        shutil.rmtree(layout.worktree_path)
        # The mirror-side admin dir may already be pruned by git; recreate still works.

        restored = await manager.ensure_worktree(
            workspace_id="ws_ensure_stale",
            repo_url=str(origin_repo),
            base_branch="development",
            new_branch="awf/ws_ensure_stale",
        )
        assert restored.worktree_path.is_dir()
        assert (restored.worktree_path / ".git").exists()

    @pytest.mark.unit
    async def test_ensure_worktree_fences_concurrent_recreate(
        self, manager: GitManager, origin_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Stale + takeover restore must not interleave remove/delete/add phases."""
        layout = await manager.add_worktree(
            workspace_id="ws_ensure_fence",
            repo_url=str(origin_repo),
            base_branch="development",
            new_branch="awf/ws_ensure_fence",
        )
        import shutil

        shutil.rmtree(layout.worktree_path)

        original_delete = manager._delete_local_branch_best_effort
        entered_delete = asyncio.Event()
        release_delete = asyncio.Event()
        delete_calls = 0

        async def _gated_delete(**kwargs: object) -> None:
            nonlocal delete_calls
            delete_calls += 1
            if delete_calls == 1:
                entered_delete.set()
                await release_delete.wait()
            await original_delete(**kwargs)

        monkeypatch.setattr(manager, "_delete_local_branch_best_effort", _gated_delete)

        task_a = asyncio.create_task(
            manager.ensure_worktree(
                workspace_id="ws_ensure_fence",
                repo_url=str(origin_repo),
                base_branch="development",
                new_branch="awf/ws_ensure_fence",
            )
        )
        await asyncio.wait_for(entered_delete.wait(), timeout=5)

        task_b = asyncio.create_task(
            manager.ensure_worktree(
                workspace_id="ws_ensure_fence",
                repo_url=str(origin_repo),
                base_branch="development",
                new_branch="awf/ws_ensure_fence",
            )
        )
        # Without a continuous fence, B can recreate while A is paused between
        # remove and add; A then deletes B's branch/checkout on resume.
        await asyncio.sleep(0.1)
        assert not task_b.done(), "takeover restore raced ahead of fenced recreate"

        release_delete.set()
        layouts = await asyncio.gather(task_a, task_b)
        assert layouts[0].worktree_path == layout.worktree_path
        assert layouts[1].worktree_path == layout.worktree_path
        assert layout.worktree_path.is_dir()
        assert (layout.worktree_path / ".git").exists()
        sha = await manager.head_sha(workspace_id="ws_ensure_fence")
        assert len(sha) == 40
