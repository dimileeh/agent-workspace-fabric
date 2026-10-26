"""Real local Git coverage for the optional expected base SHA checkout."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from awf.node.git_manager import GitManager, GitOperationError
from awf.node.git_manager_expected_base import verified_expected_base_oid


def _git(args: list[str], cwd: Path) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _init_origin(path: Path, *, object_format: str | None = None) -> None:
    args = ["init", "-q", "-b", "main"]
    if object_format is not None:
        args.extend(["--object-format", object_format])
    path.mkdir(parents=True)
    _git(args, path)
    _git(["config", "user.name", "AWF Test"], path)
    _git(["config", "user.email", "awf@test.local"], path)


def _commit(path: Path, message: str, body: str) -> str:
    (path / "README.md").write_text(body)
    _git(["add", "."], path)
    _git(["commit", "-q", "-m", message], path)
    return _git(["rev-parse", "HEAD"], path)


def _head(path: Path) -> str:
    return _git(["rev-parse", "HEAD"], path)


@pytest.fixture
def split_origin(tmp_path: Path) -> tuple[Path, str, str]:
    """Bare-capable origin with ``main`` and ``development`` at different commits."""
    repo = tmp_path / "origin"
    _init_origin(repo)
    main_sha = _commit(repo, "main", "main\n")
    _git(["checkout", "-q", "-b", "development"], repo)
    development_sha = _commit(repo, "development", "development\n")
    assert main_sha != development_sha
    return repo, main_sha, development_sha


@pytest.mark.unit
async def test_pinned_development_checks_out_that_commit_not_main(
    tmp_path: Path,
    split_origin: tuple[Path, str, str],
) -> None:
    origin, main_sha, development_sha = split_origin
    manager = GitManager(tmp_path / "awf-work")
    layout = await manager.add_worktree(
        workspace_id="ws_pin_ok",
        repo_url=str(origin),
        base_branch="development",
        new_branch="awf/ws_pin_ok",
        expected_base_sha=development_sha,
    )
    assert _head(layout.worktree_path) == development_sha
    assert _head(layout.worktree_path) != main_sha


@pytest.mark.unit
async def test_unpinned_checkout_follows_the_branch_tip(
    tmp_path: Path,
    split_origin: tuple[Path, str, str],
) -> None:
    origin, _main_sha, development_sha = split_origin
    manager = GitManager(tmp_path / "awf-work")
    first = await manager.add_worktree(
        workspace_id="ws_tip_1",
        repo_url=str(origin),
        base_branch="development",
        new_branch="awf/ws_tip_1",
        expected_base_sha=None,
    )
    assert _head(first.worktree_path) == development_sha
    moved = _commit(origin, "later", "later\n")
    second = await manager.add_worktree(
        workspace_id="ws_tip_2",
        repo_url=str(origin),
        base_branch="development",
        new_branch="awf/ws_tip_2",
    )
    assert _head(second.worktree_path) == moved


@pytest.mark.unit
async def test_moved_branch_and_wrong_sha_fail_before_worktree_add(
    tmp_path: Path,
    split_origin: tuple[Path, str, str],
) -> None:
    origin, _main_sha, development_sha = split_origin
    work = tmp_path / "awf-work"
    manager = GitManager(work)
    await manager.add_worktree(
        workspace_id="ws_seed",
        repo_url=str(origin),
        base_branch="development",
        new_branch="awf/ws_seed",
        expected_base_sha=development_sha,
    )
    _commit(origin, "moved", "moved\n")
    with pytest.raises(GitOperationError) as moved:
        await manager.add_worktree(
            workspace_id="ws_moved",
            repo_url=str(origin),
            base_branch="development",
            new_branch="awf/ws_moved",
            expected_base_sha=development_sha,
        )
    assert moved.value.reason_code == "GIT_EXPECTED_BASE_SHA_MISMATCH"
    assert not (work / "worktrees" / "ws_moved").exists()

    wrong = "e" * 40
    assert wrong != development_sha
    with pytest.raises(GitOperationError) as mismatch:
        await manager.add_worktree(
            workspace_id="ws_wrong",
            repo_url=str(origin),
            base_branch="development",
            new_branch="awf/ws_wrong",
            expected_base_sha=wrong,
        )
    assert mismatch.value.reason_code == "GIT_EXPECTED_BASE_SHA_MISMATCH"
    assert not (work / "worktrees" / "ws_wrong").exists()


@pytest.mark.unit
async def test_missing_branch_keeps_existing_reason_and_skips_worktree(
    tmp_path: Path,
    split_origin: tuple[Path, str, str],
) -> None:
    origin, _main_sha, _development_sha = split_origin
    work = tmp_path / "awf-work"
    manager = GitManager(work)
    with pytest.raises(GitOperationError) as missing:
        await manager.add_worktree(
            workspace_id="ws_missing",
            repo_url=str(origin),
            base_branch="does-not-exist",
            new_branch="awf/ws_missing",
            expected_base_sha="f" * 40,
        )
    assert missing.value.reason_code == "GIT_BASE_BRANCH_MISSING"
    assert not (work / "worktrees" / "ws_missing").exists()


@pytest.mark.unit
async def test_tag_named_development_does_not_satisfy_the_branch_pin(
    tmp_path: Path,
    split_origin: tuple[Path, str, str],
) -> None:
    origin, main_sha, development_sha = split_origin
    _git(["tag", "development", main_sha], origin)
    manager = GitManager(tmp_path / "awf-work")
    layout = await manager.add_worktree(
        workspace_id="ws_tag",
        repo_url=str(origin),
        base_branch="development",
        new_branch="awf/ws_tag",
        expected_base_sha=development_sha,
    )
    assert _head(layout.worktree_path) == development_sha
    assert _head(layout.worktree_path) != main_sha

    work = tmp_path / "awf-work"
    with pytest.raises(GitOperationError) as tag_pin:
        await manager.add_worktree(
            workspace_id="ws_tag_sha",
            repo_url=str(origin),
            base_branch="development",
            new_branch="awf/ws_tag_sha",
            expected_base_sha=main_sha,
        )
    assert tag_pin.value.reason_code == "GIT_EXPECTED_BASE_SHA_MISMATCH"
    assert not (work / "worktrees" / "ws_tag_sha").exists()


@pytest.mark.unit
async def test_non_40_hex_rev_parse_fails_closed_as_ambiguous(tmp_path: Path) -> None:
    """A SHA-256 object name is not one full 40-hex OID, so the pin fails closed."""
    origin = tmp_path / "sha256-origin"
    _init_origin(origin, object_format="sha256")
    resolved = _commit(origin, "sha256", "sha256\n")
    assert len(resolved) == 64
    work = tmp_path / "awf-work"
    manager = GitManager(work)
    with pytest.raises(GitOperationError) as ambiguous:
        await manager.add_worktree(
            workspace_id="ws_ambiguous",
            repo_url=str(origin),
            base_branch="main",
            new_branch="awf/ws_ambiguous",
            expected_base_sha="a" * 40,
        )
    assert ambiguous.value.reason_code == "GIT_EXPECTED_BASE_SHA_AMBIGUOUS"
    assert not (work / "worktrees" / "ws_ambiguous").exists()


class _GitResult:
    def __init__(self, *, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


@pytest.mark.unit
async def test_pull_head_pin_resolves_only_the_qualified_remote_commit(tmp_path: Path) -> None:
    sha = "ab" * 20
    seen: list[list[str]] = []

    async def run(args: list[str], **_kwargs: object) -> _GitResult:
        seen.append(args)
        return _GitResult(stdout=f"{sha}\n")

    resolved = await verified_expected_base_oid(
        run,
        mirror_path=tmp_path,
        base_branch="refs/pull/12/head",
        expected_base_sha=sha.upper(),
    )
    assert resolved == sha
    assert seen[0][-1] == "refs/remotes/origin/pull/12/head^{commit}"


@pytest.mark.unit
async def test_ambiguous_rev_parse_and_preclassified_pin_errors_stay_distinct(
    tmp_path: Path,
) -> None:
    sha = "cd" * 20

    async def ambiguous_stderr(args: list[str], **_kwargs: object) -> _GitResult:
        raise GitOperationError(
            operation="mirror.expected_base_sha",
            returncode=128,
            stdout="",
            stderr="warning: refname 'development' is ambiguous.",
            reason_code="GIT_COMMAND_FAILED",
        )

    with pytest.raises(GitOperationError) as ambiguous:
        await verified_expected_base_oid(
            ambiguous_stderr,
            mirror_path=tmp_path,
            base_branch="development",
            expected_base_sha=sha,
        )
    assert ambiguous.value.reason_code == "GIT_EXPECTED_BASE_SHA_AMBIGUOUS"

    async def already_classified(args: list[str], **_kwargs: object) -> _GitResult:
        raise GitOperationError(
            operation="worktree.add",
            returncode=1,
            stdout="",
            stderr="remote-tracking branch resolved to other, expected pin",
            reason_code="GIT_EXPECTED_BASE_SHA_MISMATCH",
        )

    with pytest.raises(GitOperationError) as preserved:
        await verified_expected_base_oid(
            already_classified,
            mirror_path=tmp_path,
            base_branch="development",
            expected_base_sha=sha,
        )
    assert preserved.value.reason_code == "GIT_EXPECTED_BASE_SHA_MISMATCH"

    async def warning_on_success(args: list[str], **_kwargs: object) -> _GitResult:
        return _GitResult(stdout=f"{sha}\n", stderr="warning: refname 'development' is ambiguous.")

    with pytest.raises(GitOperationError) as warned:
        await verified_expected_base_oid(
            warning_on_success,
            mirror_path=tmp_path,
            base_branch="development",
            expected_base_sha=sha,
        )
    assert warned.value.reason_code == "GIT_EXPECTED_BASE_SHA_AMBIGUOUS"
