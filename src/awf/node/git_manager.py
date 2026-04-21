"""Git mirror + worktree manager.

Wraps the ``git`` CLI via async subprocess. We intentionally use the reference
CLI (not a pure-Python git library) because:

- Worktree semantics are a first-class CLI feature and stable across git releases.
- Debugging against the CLI is easier when things go wrong on customer hardware.
- Failures produce familiar stderr strings that operators already know.

Layout the manager maintains under ``work_dir``:

    work_dir/
      mirrors/
        <repo_slug>.git/         bare mirror, shared across workspaces for this repo
      worktrees/
        <workspace_id>/          worktree checked out at a task branch
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from awf.common.logging import get_logger

_log = get_logger(__name__)


class GitOperationError(Exception):
    """Raised when a git subprocess exits non-zero or a precondition fails.

    Carries stdout/stderr so operators don't have to chase the raw command.
    """

    def __init__(
        self,
        *,
        operation: str,
        returncode: int,
        stdout: str,
        stderr: str,
        reason_code: str = "GIT_COMMAND_FAILED",
    ) -> None:
        self.operation = operation
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.reason_code = reason_code
        super().__init__(
            f"git {operation} failed (exit={returncode}, reason={reason_code}): "
            f"{stderr.strip() or stdout.strip() or '<no output>'}"
        )


@dataclass(frozen=True)
class GitResult:
    """Structured result of a git subprocess invocation."""

    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class WorktreeLayout:
    """Paths for a workspace's mirror + worktree."""

    mirror_path: Path
    worktree_path: Path
    branch_name: str


class GitManager:
    """Manages bare mirrors and per-workspace worktrees on the local filesystem."""

    def __init__(self, work_dir: Path) -> None:
        self._work_dir = work_dir
        self._mirrors_dir = work_dir / "mirrors"
        self._worktrees_dir = work_dir / "worktrees"
        # Per-repo-URL lock so two concurrent ``ensure_mirror`` calls for the same
        # repo serialize (the first clones; the second sees the mirror already
        # present and only fetches). Without this, parallel provisioning of
        # workspaces against the same repo races on the initial clone.
        self._mirror_locks: dict[str, asyncio.Lock] = {}

    @property
    def work_dir(self) -> Path:
        return self._work_dir

    # ── Public API ──────────────────────────────────────────────────────────

    async def ensure_mirror(self, repo_url: str) -> Path:
        """Ensure a bare mirror for ``repo_url`` exists and is up to date.

        Clones on first call; fetches on subsequent calls. Returns the mirror path.
        Concurrent calls for the same ``repo_url`` are serialized so the initial
        clone doesn't race.
        """
        self._mirrors_dir.mkdir(parents=True, exist_ok=True)
        mirror_path = self._mirror_path(repo_url)
        lock = self._mirror_locks.setdefault(repo_url, asyncio.Lock())

        async with lock:
            if mirror_path.exists():
                await self._run(
                    ["git", "--git-dir", str(mirror_path), "remote", "update", "--prune"],
                    operation="mirror.update",
                )
                return mirror_path

            await self._run(
                ["git", "clone", "--mirror", repo_url, str(mirror_path)],
                operation="mirror.clone",
            )
            return mirror_path

    async def add_worktree(
        self, *, workspace_id: str, repo_url: str, base_branch: str, new_branch: str
    ) -> WorktreeLayout:
        """Create a fresh worktree for ``workspace_id`` at a new branch off ``base_branch``.

        Raises ``GitOperationError`` with a specific reason code if:
        - the base branch doesn't exist (``GIT_BASE_BRANCH_MISSING``)
        - the worktree path already exists (``GIT_WORKTREE_ALREADY_EXISTS``)

        The mirror lock is held around ``rev-parse`` + ``worktree add`` because
        git's internal worktree registry is a shared file inside the mirror; two
        concurrent ``worktree add`` invocations on the same mirror occasionally
        corrupt each other's HEAD metadata (observed in CI). Single-node
        serialization is cheap enough for the MVP; Phase 1.5 can revisit with
        per-worktree locks if throughput becomes a concern.
        """
        mirror_path = await self.ensure_mirror(repo_url)
        self._worktrees_dir.mkdir(parents=True, exist_ok=True)

        worktree_path = self._worktrees_dir / workspace_id
        if worktree_path.exists():
            raise GitOperationError(
                operation="worktree.add",
                returncode=1,
                stdout="",
                stderr=f"worktree path already exists: {worktree_path}",
                reason_code="GIT_WORKTREE_ALREADY_EXISTS",
            )

        lock = self._mirror_locks.setdefault(repo_url, asyncio.Lock())
        async with lock:
            try:
                await self._run(
                    [
                        "git",
                        "--git-dir",
                        str(mirror_path),
                        "rev-parse",
                        "--verify",
                        base_branch,
                    ],
                    operation="mirror.rev-parse",
                )
            except GitOperationError as exc:
                raise GitOperationError(
                    operation="worktree.add",
                    returncode=exc.returncode,
                    stdout=exc.stdout,
                    stderr=exc.stderr,
                    reason_code="GIT_BASE_BRANCH_MISSING",
                ) from exc

            await self._run(
                [
                    "git",
                    "--git-dir",
                    str(mirror_path),
                    "worktree",
                    "add",
                    "-b",
                    new_branch,
                    str(worktree_path),
                    base_branch,
                ],
                operation="worktree.add",
            )

        return WorktreeLayout(
            mirror_path=mirror_path,
            worktree_path=worktree_path,
            branch_name=new_branch,
        )

    async def remove_worktree(self, *, workspace_id: str, repo_url: str) -> None:
        """Remove a worktree idempotently. Missing worktrees are not errors.

        We also run ``worktree prune`` so metadata is cleaned up even if the
        directory was deleted out-of-band.
        """
        mirror_path = self._mirror_path(repo_url)
        worktree_path = self._worktrees_dir / workspace_id

        if worktree_path.exists():
            # ``--force`` because a failed task may leave dirty state.
            await self._run(
                [
                    "git",
                    "--git-dir",
                    str(mirror_path),
                    "worktree",
                    "remove",
                    "--force",
                    str(worktree_path),
                ],
                operation="worktree.remove",
            )

        if mirror_path.exists():
            await self._run(
                ["git", "--git-dir", str(mirror_path), "worktree", "prune"],
                operation="worktree.prune",
            )

    async def head_sha(self, *, workspace_id: str) -> str:
        """Return the current HEAD SHA of the workspace's worktree.

        Used for event metadata + base-commit recording. The workspace ID
        uniquely identifies the worktree path, so we don't need repo_url here.
        """
        worktree_path = self._worktrees_dir / workspace_id
        result = await self._run(
            ["git", "-C", str(worktree_path), "rev-parse", "HEAD"],
            operation="worktree.head",
        )
        return result.stdout.strip()

    # ── Internals ───────────────────────────────────────────────────────────

    def _mirror_path(self, repo_url: str) -> Path:
        """Derive a filesystem-safe mirror name from the repo URL.

        We combine a slugified repo name (for human readability when operators
        poke at the filesystem) with a short hash of the full URL (for uniqueness
        across forks that share a name).
        """
        slug = _slugify_repo(repo_url)
        digest = hashlib.sha256(repo_url.encode("utf-8")).hexdigest()[:12]
        return self._mirrors_dir / f"{slug}-{digest}.git"

    async def _run(self, args: list[str], *, operation: str) -> GitResult:
        _log.debug("git.exec", operation=operation, args=args)
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await proc.communicate()
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")

        assert proc.returncode is not None  # always set after communicate()

        if proc.returncode != 0:
            raise GitOperationError(
                operation=operation,
                returncode=proc.returncode,
                stdout=stdout,
                stderr=stderr,
            )
        return GitResult(returncode=proc.returncode, stdout=stdout, stderr=stderr)


_SLUG_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _slugify_repo(repo_url: str) -> str:
    """Produce a short readable piece of a repo URL for filesystem naming.

    We take the last path segment (typically ``owner/name.git``) and sanitize it.
    The SHA suffix added by the caller ensures uniqueness.
    """
    tail = repo_url.rstrip("/").split("/")[-1]
    if tail.endswith(".git"):
        tail = tail[:-4]
    return _SLUG_RE.sub("-", tail) or "repo"
