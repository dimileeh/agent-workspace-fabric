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
import weakref
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

    # Lock registry scoped by event loop. Must be class-level, not
    # instance-level: ``scripts/run_awf.py`` constructs one ``GitManager``
    # per task inside ``asyncio.gather(...)``. If the dict were an
    # instance attribute, two concurrent tasks targeting the same repo
    # would get INDEPENDENT locks and race on ``git clone --mirror`` /
    # ``worktree add`` / ``worktree prune`` — the exact corruption the
    # lock exists to prevent.
    #
    # We key first by running loop, then by resolved mirror path, rather
    # than keeping a flat ``{path → Lock}`` dict, because ``asyncio.Lock``
    # binds to its creating loop on first acquire and re-acquiring from a
    # different loop raises ``RuntimeError``. Production runs one
    # ``asyncio.run``, but pytest-asyncio creates a fresh loop per test —
    # a flat registry would hand the second test a stale lock from the
    # first. The WeakKeyDictionary drops the inner dict automatically
    # when a loop is garbage-collected. ``mirror_path.resolve()`` as the
    # inner key ensures symlinks / relative-vs-absolute forms of the
    # same physical path share a lock.
    _mirror_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]] = (
        weakref.WeakKeyDictionary()
    )

    def __init__(self, work_dir: Path) -> None:
        self._work_dir = work_dir
        self._mirrors_dir = work_dir / "mirrors"
        self._worktrees_dir = work_dir / "worktrees"

    @classmethod
    def _lock_for_mirror(cls, mirror_path: Path) -> asyncio.Lock:
        """Return the per-loop lock for ``mirror_path``, creating it on
        first use. Safe across multiple ``asyncio.run`` invocations
        (tests, multi-loop callers) because each loop gets its own
        inner dict and the inner dict + its Locks are GC'd when the
        loop goes away."""
        loop = asyncio.get_running_loop()
        loop_locks = cls._mirror_locks.get(loop)
        if loop_locks is None:
            loop_locks = {}
            cls._mirror_locks[loop] = loop_locks
        return loop_locks.setdefault(str(mirror_path.resolve()), asyncio.Lock())

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
        lock = self._lock_for_mirror(mirror_path)

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
            # ``git clone --mirror`` sets ``remote.origin.mirror=true`` which
            # refuses refspec pushes from any worktree attached to this bare
            # repo (``fatal: --mirror can't be combined with refspecs``). Strip
            # the flag so individual worktrees can push their feature branches
            # normally.
            await self._run(
                [
                    "git",
                    "--git-dir",
                    str(mirror_path),
                    "config",
                    "--unset",
                    "remote.origin.mirror",
                ],
                operation="mirror.strip_mirror_flag",
            )
            # Stripping ``mirror=true`` is not enough on its own. ``git clone
            # --mirror`` also sets the fetch refspec to ``+refs/*:refs/*`` —
            # every remote ref lives directly under ``refs/heads/*`` locally,
            # and ``git remote update --prune`` will therefore DELETE any
            # local branch that isn't on the remote. That includes the AWF
            # feature branches we create with ``git worktree add -b``. When
            # two workspaces run in parallel against the same repo (or when
            # companion materialisation triggers ``remote update --prune``
            # on the main workspace's mirror), a pruned branch ref turns the
            # worktree's HEAD into a dangling symbolic reference, and the
            # next commit becomes an orphan root — which then fails
            # ``gh pr create`` with "no history in common". Switch to a
            # standard non-mirror refspec so prune only affects remote
            # tracking refs (``refs/remotes/origin/*``), never local branches.
            await self._run(
                [
                    "git",
                    "--git-dir",
                    str(mirror_path),
                    "config",
                    "remote.origin.fetch",
                    "+refs/heads/*:refs/remotes/origin/*",
                ],
                operation="mirror.rewrite_fetch_refspec",
            )
            # The initial clone already populated refs/heads/* with every
            # server branch. Those will go stale once the refspec changes
            # (future fetches only update refs/remotes/origin/*). Delete them
            # so the only heads in the mirror are AWF-created feature
            # branches, and ``origin/<branch>`` is the canonical up-to-date
            # tip for resolving base commits.
            listing = await self._run(
                [
                    "git",
                    "--git-dir",
                    str(mirror_path),
                    "for-each-ref",
                    "--format=%(refname)",
                    "refs/heads/",
                ],
                operation="mirror.list_local_heads",
            )
            for ref in (line.strip() for line in listing.stdout.splitlines()):
                if not ref:
                    continue
                await self._run(
                    [
                        "git",
                        "--git-dir",
                        str(mirror_path),
                        "update-ref",
                        "-d",
                        ref,
                    ],
                    operation="mirror.delete_stale_local_head",
                )
            # Fetch fresh remote-tracking refs so ``origin/<branch>`` works.
            await self._run(
                [
                    "git",
                    "--git-dir",
                    str(mirror_path),
                    "fetch",
                    "origin",
                    "--prune",
                ],
                operation="mirror.initial_fetch_tracking",
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

        # ``ensure_mirror`` now only tracks ``refs/remotes/origin/*`` — local
        # heads are reserved for AWF-created feature branches. Base-branch
        # lookups must therefore go through the remote-tracking ref so we
        # see the latest server tip even across long-running sessions, and
        # so ``remote update --prune`` (which prunes only tracking refs
        # under the new refspec) never targets the worktree's own branch.
        tracking_ref = f"origin/{base_branch}"

        lock = self._lock_for_mirror(mirror_path)
        async with lock:
            try:
                await self._run(
                    [
                        "git",
                        "--git-dir",
                        str(mirror_path),
                        "rev-parse",
                        "--verify",
                        tracking_ref,
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
                    tracking_ref,
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

        The per-mirror lock is held for both operations because the mirror's
        ``$GIT_DIR/worktrees/`` admin dir is a single file git mutates on
        every worktree add / remove / prune. Without the lock, a concurrent
        ``add_worktree`` (e.g. one workspace tearing down while another
        provisions against the same mirror) can see a half-pruned registry
        and end up with a dangling worktree entry or a corrupted HEAD ref.
        """
        mirror_path = self._mirror_path(repo_url)
        worktree_path = self._worktrees_dir / workspace_id

        lock = self._lock_for_mirror(mirror_path)
        async with lock:
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
