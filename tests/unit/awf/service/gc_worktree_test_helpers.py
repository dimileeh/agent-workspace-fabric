from __future__ import annotations

from pathlib import Path


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _git(args: list[str], cwd: Path) -> None:
    import subprocess

    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _make_mirror_with_worktree(tmp_path: Path, work_dir: Path, workspace_id: str) -> str:
    import subprocess

    origin = tmp_path / "origin"
    origin.mkdir()
    _git(["init", "-q", "-b", "main"], origin)
    _git(["config", "user.name", "AWF Test"], origin)
    _git(["config", "user.email", "awf@test.local"], origin)
    (origin / "README.md").write_text("initial\n", encoding="utf-8")
    _git(["add", "."], origin)
    _git(["commit", "-q", "-m", "init"], origin)
    repo_url = str(origin)
    mirror = work_dir / "git" / "mirrors" / "repo.git"
    mirror.parent.mkdir(parents=True)
    subprocess.run(
        ["git", "clone", "--bare", repo_url, str(mirror)],
        check=True,
        capture_output=True,
    )
    worktree = work_dir / "git" / "worktrees" / workspace_id
    worktree.parent.mkdir(parents=True)
    subprocess.run(
        ["git", "--git-dir", str(mirror), "worktree", "add", str(worktree), "main"],
        check=True,
        capture_output=True,
    )
    return repo_url
