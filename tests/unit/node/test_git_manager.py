"""GitManager tests against a real throwaway git repository.

We intentionally exercise the real ``git`` CLI rather than mock it — the whole
value of GitManager is correct handling of real worktree semantics, so mocking
would test nothing useful. Setup is fast (git init + two commits < 200 ms on
typical hardware) and each test gets an isolated ``tmp_path``.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

import awf.node.git_manager as git_manager
from awf.node import git_manager as git_module
from awf.node.git_manager import (
    GitManager,
    GitOperationError,
    _agent_writable_git_targets,
)


@pytest.mark.unit
def test_github_pull_head_ref_pattern_matches_expected() -> None:
    pattern = git_manager._GITHUB_PULL_HEAD_REF
    assert pattern.match("refs/pull/278/head")
    assert pattern.match("refs/pull/0/head") is None


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
def test_agent_writable_git_targets_handle_missing_optional_paths(tmp_path: Path) -> None:
    mirror = tmp_path / "mirror.git"
    worktree = tmp_path / "worktree"
    mirror.mkdir()
    worktree.mkdir()

    targets = git_manager._agent_writable_git_targets(  # noqa: SLF001
        layout_mirror=mirror,
        worktree_path=worktree,
    )

    assert targets == (
        git_manager._ChownTarget(worktree, recursive=True),  # noqa: SLF001
        git_manager._ChownTarget(mirror, recursive=False),  # noqa: SLF001
    )


@pytest.mark.unit
def test_linked_worktree_git_dir_handles_invalid_relative_and_unreadable_gitfiles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    assert git_manager.linked_worktree_git_dir(worktree) is None

    git_file = worktree / ".git"
    git_file.write_text("not-a-gitdir")
    assert git_manager.linked_worktree_git_dir(worktree) is None

    git_file.write_text("gitdir: ../mirror.git/worktrees/ws")
    assert (
        git_manager.linked_worktree_git_dir(worktree)
        == (worktree / "../mirror.git/worktrees/ws").resolve()
    )

    original_read_text = Path.read_text

    def _raise_for_git_file(path: Path, *args: object, **kwargs: object) -> str:
        if path == git_file:
            raise OSError("unreadable")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _raise_for_git_file)
    assert git_manager.linked_worktree_git_dir(worktree) is None


@pytest.mark.unit
def test_chown_targets_skips_duplicates_and_missing_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = tmp_path / "existing"
    existing.write_text("ok")
    missing = tmp_path / "missing"
    chowned: list[Path] = []

    monkeypatch.setattr(
        os,
        "chown",
        lambda path, _uid, _gid: chowned.append(Path(path)),
    )

    git_manager._chown_targets(  # noqa: SLF001
        (
            git_manager._ChownTarget(existing, recursive=False),  # noqa: SLF001
            git_manager._ChownTarget(existing, recursive=False),  # noqa: SLF001
            git_manager._ChownTarget(missing, recursive=False),  # noqa: SLF001
        ),
        1000,
        1000,
    )

    assert chowned == [existing]


@pytest.mark.unit
def test_chown_targets_uses_lchown_for_non_recursive_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "outside-target"
    target.mkdir()
    linked = tmp_path / "mirror-worktrees"
    linked.symlink_to(target, target_is_directory=True)
    chowned: list[Path] = []
    lchowned: list[Path] = []

    monkeypatch.setattr(
        os,
        "chown",
        lambda path, _uid, _gid: chowned.append(Path(path)),
    )
    monkeypatch.setattr(
        os,
        "lchown",
        lambda path, _uid, _gid: lchowned.append(Path(path)),
    )

    git_manager._chown_targets(  # noqa: SLF001
        (git_manager._ChownTarget(linked, recursive=False),),  # noqa: SLF001
        1000,
        1000,
    )

    assert chowned == []
    assert lchowned == [linked]


@pytest.mark.unit
def test_chown_targets_uses_lchown_for_dangling_non_recursive_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    linked = tmp_path / "mirror-worktrees"
    linked.symlink_to(tmp_path / "missing-target", target_is_directory=True)
    chowned: list[Path] = []
    lchowned: list[Path] = []

    monkeypatch.setattr(
        os,
        "chown",
        lambda path, _uid, _gid: chowned.append(Path(path)),
    )
    monkeypatch.setattr(
        os,
        "lchown",
        lambda path, _uid, _gid: lchowned.append(Path(path)),
    )

    git_manager._chown_targets(  # noqa: SLF001
        (git_manager._ChownTarget(linked, recursive=False),),  # noqa: SLF001
        1000,
        1000,
    )

    assert chowned == []
    assert lchowned == [linked]


@pytest.mark.unit
def test_chown_tree_returns_after_chowning_plain_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    file_path = tmp_path / "plain-file"
    file_path.write_text("ok")
    chowned: list[Path] = []

    monkeypatch.setattr(
        os,
        "chown",
        lambda path, _uid, _gid: chowned.append(Path(path)),
    )

    git_manager._chown_tree(file_path, 1000, 1000)  # noqa: SLF001

    assert chowned == [file_path]


@pytest.mark.unit
def test_chown_tree_directories_only_repairs_object_fanout_dirs_not_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    objects = tmp_path / "objects"
    fanout = objects / "c4"
    pack = objects / "pack"
    fanout.mkdir(parents=True)
    pack.mkdir()
    loose_object = fanout / "abcdef"
    loose_object.write_text("object\n", encoding="utf-8")
    pack_file = pack / "pack-test.pack"
    pack_file.write_text("pack\n", encoding="utf-8")
    chowned: list[Path] = []

    monkeypatch.setattr(
        os,
        "chown",
        lambda path, _uid, _gid: chowned.append(Path(path)),
    )

    git_manager._chown_tree(objects, 1000, 1000, directories_only=True)  # noqa: SLF001

    assert objects in chowned
    assert fanout in chowned
    assert pack in chowned
    assert loose_object not in chowned
    assert pack_file not in chowned


@pytest.mark.unit
def test_chown_tree_skips_symlink_targets_using_lchown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "target"
    target.write_text("target", encoding="utf-8")
    root = tmp_path / "symlink-root"
    root.symlink_to(target)
    linked_target = tmp_path / "linked-target"
    linked_target.write_text("linked-target", encoding="utf-8")
    directory = tmp_path / "worktree"
    directory.mkdir()
    linked_child = directory / "linked-child"
    linked_child.symlink_to(linked_target)
    child_file = directory / "file"
    child_file.write_text("file", encoding="utf-8")
    chowned: list[Path] = []
    lchowned: list[Path] = []

    def _record_chown(path: str | bytes, _uid: int, _gid: int) -> None:
        del _uid, _gid
        chowned.append(Path(path))

    def _record_lchown(path: str | bytes, _uid: int, _gid: int) -> None:
        del _uid, _gid
        lchowned.append(Path(path))

    monkeypatch.setattr(git_manager.os, "chown", _record_chown)
    monkeypatch.setattr(git_manager.os, "lchown", _record_lchown)

    git_manager._chown_tree(root, 1000, 1000)  # noqa: SLF001
    git_manager._chown_tree(directory, 1000, 1000)  # noqa: SLF001

    assert set(lchowned) == {root, linked_child}
    assert set(chowned) >= {directory, child_file}
    assert target not in chowned
    assert linked_target not in chowned


@pytest.mark.unit
def test_repair_agent_writable_worktree_falls_back_when_mirror_is_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    captured: list[tuple[tuple[git_manager._ChownTarget, ...], int, int]] = []  # noqa: SLF001

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        git_manager,
        "_chown_targets",
        lambda targets, uid, gid: captured.append((targets, uid, gid)),
    )

    git_manager.repair_agent_writable_worktree(None, worktree, uid=123, gid=456)

    assert captured == [
        ((git_manager._ChownTarget(worktree, recursive=True),), 123, 456)  # noqa: SLF001
    ]


@pytest.mark.unit
def test_repair_agent_writable_worktree_fallback_repairs_linked_git_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    linked_git_dir = tmp_path / "mirror.git" / "worktrees" / "ws"
    linked_git_dir.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {linked_git_dir}\n", encoding="utf-8")
    captured: list[tuple[git_manager._ChownTarget, ...]] = []  # noqa: SLF001

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(git_manager, "mirror_path_for_worktree", lambda _path: None)
    monkeypatch.setattr(
        git_manager,
        "_chown_targets",
        lambda targets, _uid, _gid: captured.append(targets),
    )

    git_manager.repair_agent_writable_worktree(None, worktree)

    assert captured == [
        (
            git_manager._ChownTarget(worktree, recursive=True),  # noqa: SLF001
            git_manager._ChownTarget(linked_git_dir, recursive=True),  # noqa: SLF001
        )
    ]


@pytest.mark.unit
def test_repair_agent_writable_worktree_repairs_runtime_venv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "worktree"
    venv_bin = worktree / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    uv = venv_bin / "uv"
    uv.write_text("#!/bin/sh\n", encoding="utf-8")
    chowned: list[Path] = []

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(git_manager, "mirror_path_for_worktree", lambda _path: None)
    monkeypatch.setattr(
        os,
        "chown",
        lambda path, _uid, _gid: chowned.append(Path(path)),
    )

    git_manager.repair_agent_writable_worktree(None, worktree)

    assert worktree in chowned
    assert worktree / ".venv" in chowned
    assert venv_bin in chowned
    assert uv in chowned


@pytest.mark.unit
def test_mirror_path_for_worktree_handles_commondir_and_unreadable_commondir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mirror = tmp_path / "mirror.git"
    linked_git_dir = mirror / "worktrees" / "ws"
    linked_git_dir.mkdir(parents=True)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {linked_git_dir}\n", encoding="utf-8")

    assert git_manager.mirror_path_for_worktree(worktree) == mirror.resolve()

    (linked_git_dir / "commondir").write_text("../..", encoding="utf-8")
    assert git_manager.mirror_path_for_worktree(worktree) == mirror.resolve()

    absolute_common_dir = tmp_path / "absolute-common.git"
    absolute_common_dir.mkdir()
    (linked_git_dir / "commondir").write_text(str(absolute_common_dir), encoding="utf-8")
    assert git_manager.mirror_path_for_worktree(worktree) == absolute_common_dir.resolve()

    original_read_text = Path.read_text

    def _raise_for_commondir(path: Path, *args: object, **kwargs: object) -> str:
        if path == linked_git_dir / "commondir":
            raise OSError("unreadable")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _raise_for_commondir)
    assert git_manager.mirror_path_for_worktree(worktree) == mirror.resolve()

    no_git = tmp_path / "no-git"
    no_git.mkdir()
    assert git_manager.mirror_path_for_worktree(no_git) is None


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
    @pytest.mark.unit
    async def test_removes_existing_worktree(self, manager: GitManager, origin_repo: Path) -> None:
        layout = await manager.add_worktree(
            workspace_id="ws_rm",
            repo_url=str(origin_repo),
            base_branch="development",
            new_branch="awf/ws_rm",
        )
        assert layout.worktree_path.exists()

        await manager.remove_worktree(workspace_id="ws_rm", repo_url=str(origin_repo))
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


class TestHeadSha:
    @pytest.mark.unit
    async def test_returns_current_head(self, manager: GitManager, origin_repo: Path) -> None:
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


class TestGitEnvironment:
    @pytest.mark.unit
    async def test_run_uses_configured_environment(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        manager = GitManager(tmp_path / "work", env={"HOME": str(home), "AWF_TEST_ENV": "ok"})

        result = await manager._run(  # noqa: SLF001 - narrow regression for subprocess env.
            ["sh", "-c", 'printf \'%s:%s\' "$HOME" "$AWF_TEST_ENV"'],
            operation="env",
        )

        assert result.stdout == f"{home}:ok"


class TestAgentWritableWorktreeHelpers:
    @pytest.mark.unit
    async def test_prepare_agent_writable_worktree_skips_chown_when_not_root(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(os, "geteuid", lambda: 1000)

        async def _unexpected_to_thread(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("non-root process must not chown worktrees")

        monkeypatch.setattr(git_module.asyncio, "to_thread", _unexpected_to_thread)
        manager = GitManager(
            tmp_path / "awf-work",
            worktree_owner_uid=1000,
            worktree_owner_gid=1000,
        )

        await manager._prepare_agent_writable_worktree(  # noqa: SLF001
            layout_mirror=tmp_path / "mirror.git",
            worktree_path=tmp_path / "worktree",
        )

    @pytest.mark.unit
    def test_linked_worktree_git_dir_handles_absent_unreadable_and_relative_gitfiles(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        missing_gitfile = tmp_path / "missing"
        missing_gitfile.mkdir()
        assert git_module.linked_worktree_git_dir(missing_gitfile) is None

        unreadable = tmp_path / "unreadable"
        unreadable.mkdir()
        unreadable_gitfile = unreadable / ".git"
        unreadable_gitfile.write_text("gitdir: ../real.git\n", encoding="utf-8")
        original_read_text = Path.read_text

        def _read_text(path: Path, *args: object, **kwargs: object) -> str:
            if path == unreadable_gitfile:
                raise OSError("cannot read gitfile")
            return original_read_text(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", _read_text)
        assert git_module.linked_worktree_git_dir(unreadable) is None

        malformed = tmp_path / "malformed"
        malformed.mkdir()
        (malformed / ".git").write_text("not a gitdir pointer\n", encoding="utf-8")
        assert git_module.linked_worktree_git_dir(malformed) is None

        relative = tmp_path / "relative"
        relative.mkdir()
        (relative / ".git").write_text(
            "gitdir: ../mirror.git/worktrees/ws_relative\n",
            encoding="utf-8",
        )

        assert (
            git_module.linked_worktree_git_dir(relative)
            == (relative / "../mirror.git/worktrees/ws_relative").resolve()
        )

    @pytest.mark.unit
    def test_chown_targets_skip_missing_and_duplicate_paths(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        chowned: list[tuple[Path, int, int]] = []
        file_path = tmp_path / "owned-file"
        file_path.write_text("content\n", encoding="utf-8")
        missing_path = tmp_path / "missing"

        monkeypatch.setattr(
            os,
            "chown",
            lambda path, uid, gid: chowned.append((Path(path), uid, gid)),
        )

        git_module._chown_targets(  # noqa: SLF001
            (
                git_module._ChownTarget(file_path, recursive=True),  # noqa: SLF001
                git_module._ChownTarget(file_path, recursive=True),  # noqa: SLF001
                git_module._ChownTarget(missing_path, recursive=False),  # noqa: SLF001
            ),
            1000,
            1001,
        )

        assert chowned == [(file_path, 1000, 1001)]
