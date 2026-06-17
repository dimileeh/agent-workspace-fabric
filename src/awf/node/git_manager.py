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
import os
import re
import shutil
import weakref
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from awf.common.git_auth import GitAuthNotConfiguredError, verify_bitbucket_git_auth
from awf.common.git_identity import git_safe_directory_config_args
from awf.common.logging import get_logger

_log = get_logger(__name__)

_GITHUB_PULL_HEAD_REF = re.compile(r"^refs/pull/([1-9][0-9]*)/head$")
AGENT_RUNTIME_UID = 1000
AGENT_RUNTIME_GID = 1000


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


@dataclass(frozen=True)
class _ChownTarget:
    path: Path
    recursive: bool
    directories_only: bool = False


class GitManager:
    """Manages bare mirrors and per-workspace worktrees on the local filesystem."""

    # Lock registry scoped by event loop. Must be class-level, not
    # instance-level: the worker can provision multiple tasks concurrently.
    # If the dict were an instance attribute, two concurrent tasks targeting
    # the same repo would get independent locks and race on ``git clone
    # --mirror`` / ``worktree add`` / ``worktree prune``.
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

    def __init__(
        self,
        work_dir: Path,
        *,
        env: Mapping[str, str] | None = None,
        worktree_owner_uid: int | None = None,
        worktree_owner_gid: int | None = None,
    ) -> None:
        self._work_dir = work_dir
        self._mirrors_dir = work_dir / "mirrors"
        self._worktrees_dir = work_dir / "worktrees"
        self._env = {**os.environ, **env} if env is not None else None
        self._worktree_owner_uid = worktree_owner_uid
        self._worktree_owner_gid = worktree_owner_gid

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

    def get_worktree_path(self, workspace_id: str) -> Path:
        """Return the managed worktree path for ``workspace_id``."""
        return self._worktrees_dir / workspace_id

    async def ensure_mirror(self, repo_url: str) -> Path:
        """Ensure a bare mirror for ``repo_url`` exists and is up to date.

        Clones on first call; fetches on subsequent calls. Returns the mirror path.
        Concurrent calls for the same ``repo_url`` are serialized so the initial
        clone doesn't race.

        For a bitbucket.org repo, a credential preflight runs first: if the
        Bitbucket git credentials are not configured it raises a reason-coded
        ``GitOperationError`` instead of attempting an unauthenticated clone of a
        private repo (which would fail opaquely or hang). GitHub repos are
        unaffected.
        """
        self._mirrors_dir.mkdir(parents=True, exist_ok=True)
        mirror_path = self._mirror_path(repo_url)
        # Label the preflight failure with the operation ``ensure_mirror`` would
        # actually attempt: an existing mirror only fetches (``mirror.update``),
        # so a credential failure there must not masquerade as a first-time clone.
        self._bitbucket_auth_preflight(
            repo_url,
            operation="mirror.update" if mirror_path.exists() else "mirror.clone",
        )
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

        ``base_branch`` is normally a branch name. Adopted GitHub PR workspaces
        may pass ``refs/pull/<number>/head`` to check out the exact PR head.

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
        tracking_ref, fetch_refspec = _checkout_tracking_ref(base_branch)

        lock = self._lock_for_mirror(mirror_path)
        async with lock:
            try:
                if fetch_refspec is not None:
                    await self._run(
                        [
                            "git",
                            "--git-dir",
                            str(mirror_path),
                            "fetch",
                            "origin",
                            fetch_refspec,
                        ],
                        operation="mirror.fetch_checkout_ref",
                    )
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

        await self._prepare_agent_writable_worktree(
            layout_mirror=mirror_path,
            worktree_path=worktree_path,
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
                try:
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
                except GitOperationError as exc:
                    # Idempotent removal: a directory left behind with stale git
                    # metadata makes ``git worktree remove`` fail with
                    # ``fatal: '<path>' is not a working tree``. That is an
                    # already-removed condition from git's point of view, not a
                    # failure. Re-raise any genuine removal error (we match only
                    # this condition).
                    if "is not a working tree" not in exc.stderr.lower():
                        raise
                    # ``git worktree remove`` never ran, so the physical
                    # directory and its contents are still on disk; ``worktree
                    # prune`` below only clears metadata for *missing* dirs and
                    # would leave the disk space behind. Reclaim it ourselves so
                    # GC actually frees the space it reports as reclaimed. We must
                    # NOT swallow genuine deletion failures (e.g. permission
                    # errors): if rmtree fails the directory is still on disk, and
                    # callers like ``WorkspaceCleanupService.cleanup_workspace``
                    # rely on a raised error to record the step as partial/failed
                    # instead of falsely reporting removal success.
                    await asyncio.to_thread(self._reclaim_stale_worktree, worktree_path)
                    _log.info(
                        "worktree.remove.already_gone",
                        workspace_id=workspace_id,
                        worktree_path=str(worktree_path),
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

    @staticmethod
    def _reclaim_stale_worktree(worktree_path: Path) -> None:
        """Delete a leftover worktree directory, surfacing genuine failures.

        Runs in a worker thread. A concurrent remover may win the race and
        delete the directory first; that already-gone case is success. Any
        other ``OSError`` (permissions, read-only filesystem) means the disk
        space was *not* reclaimed, so we raise ``GitOperationError`` to keep
        cleanup honest rather than silently leaking the directory.
        """
        try:
            shutil.rmtree(worktree_path)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise GitOperationError(
                operation="worktree.remove",
                returncode=1,
                stdout="",
                stderr=f"failed to reclaim stale worktree dir {worktree_path}: {exc}",
                reason_code="GIT_WORKTREE_REMOVE_FAILED",
            ) from exc

    def _mirror_path(self, repo_url: str) -> Path:
        """Derive a filesystem-safe mirror name from the repo URL.

        We combine a slugified repo name (for human readability when operators
        poke at the filesystem) with a short hash of the full URL (for uniqueness
        across forks that share a name).
        """
        slug = _slugify_repo(repo_url)
        digest = hashlib.sha256(repo_url.encode("utf-8")).hexdigest()[:12]
        return self._mirrors_dir / f"{slug}-{digest}.git"

    def _bitbucket_auth_preflight(self, repo_url: str, *, operation: str) -> None:
        """Fail fast with a reason code when a bitbucket.org repo lacks git creds.

        Reads the manager's git env (which the worker populates with the live
        process environment), so it sees the same ``BITBUCKET_*`` credentials the
        credential helper would use. No-op for non-bitbucket repos. ``operation``
        labels the git step the caller would otherwise have run (clone vs update)
        so the reason-coded failure is not misread as a first-time clone.
        """
        try:
            verify_bitbucket_git_auth(repo_url, self._env if self._env is not None else os.environ)
        except GitAuthNotConfiguredError as exc:
            raise GitOperationError(
                operation=operation,
                returncode=128,
                stdout="",
                stderr=str(exc),
                reason_code=exc.reason_code,
            ) from exc

    async def _run(self, args: list[str], *, operation: str) -> GitResult:
        _log.debug("git.exec", operation=operation, args=args)
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._env,
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

    async def _prepare_agent_writable_worktree(
        self,
        *,
        layout_mirror: Path,
        worktree_path: Path,
    ) -> None:
        """Make a root-created linked worktree usable by the agent-runtime user."""
        if self._worktree_owner_uid is None or self._worktree_owner_gid is None:
            return
        if os.geteuid() != 0:
            return
        await asyncio.to_thread(
            repair_agent_writable_worktree,
            layout_mirror,
            worktree_path,
            self._worktree_owner_uid,
            self._worktree_owner_gid,
        )


_SLUG_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _checkout_tracking_ref(base_branch: str) -> tuple[str, str | None]:
    pull_ref = _GITHUB_PULL_HEAD_REF.fullmatch(base_branch)
    if pull_ref is None:
        return f"origin/{base_branch}", None

    pr_number = pull_ref.group(1)
    tracking_ref = f"refs/remotes/origin/pull/{pr_number}/head"
    return tracking_ref, f"+refs/pull/{pr_number}/head:{tracking_ref}"


def _slugify_repo(repo_url: str) -> str:
    """Produce a short readable piece of a repo URL for filesystem naming.

    We take the last path segment (typically ``owner/name.git``) and sanitize it.
    The SHA suffix added by the caller ensures uniqueness.
    """
    tail = repo_url.rstrip("/").split("/")[-1]
    if tail.endswith(".git"):
        tail = tail[:-4]
    return _SLUG_RE.sub("-", tail) or "repo"


def _agent_writable_git_targets(
    *,
    layout_mirror: Path,
    worktree_path: Path,
    linked_git_dir: Path | None = None,
) -> tuple[_ChownTarget, ...]:
    targets = [_ChownTarget(worktree_path, recursive=True)]
    if linked_git_dir is None:
        linked_git_dir = linked_worktree_git_dir(worktree_path)
    if linked_git_dir is not None:
        targets.append(_ChownTarget(linked_git_dir, recursive=True))
    targets.append(_ChownTarget(layout_mirror, recursive=False))
    objects = layout_mirror / "objects"
    if objects.exists():
        # Git's object database is special: loose object files are normally
        # read-only and only need to be readable, while the fanout directories
        # must be writable so the agent can add new objects. Recursively
        # chowning object files breaks on Docker Desktop/macOS when a host file
        # lacks Docker ownership metadata and appears as unwritable root:root in
        # the control-plane container. Linux still gets writable object dirs.
        targets.append(_ChownTarget(objects, recursive=True, directories_only=True))
    # Linked worktrees install hooks in the shared bare mirror; setup commands
    # such as ``pre-commit install`` must be able to write there.
    for child in ("hooks", "refs", "logs"):
        candidate = layout_mirror / child
        if candidate.exists():
            targets.append(_ChownTarget(candidate, recursive=True))
    worktrees = layout_mirror / "worktrees"
    if worktrees.exists():
        targets.append(_ChownTarget(worktrees, recursive=False))
    return tuple(targets)


def repair_agent_writable_worktree(
    layout_mirror: Path | None,
    worktree_path: Path,
    uid: int = AGENT_RUNTIME_UID,
    gid: int = AGENT_RUNTIME_GID,
    linked_git_dir: Path | None = None,
) -> None:
    """Repair linked-worktree Git ownership for the agent-runtime user.

    The mirror object DB is intentionally repaired in directories-only mode:
    loose object files and pack files may be immutable through Docker Desktop
    on macOS, while fanout directories must be writable on Linux so the agent
    can add new objects.
    """
    if os.geteuid() != 0:
        return
    mirror = layout_mirror or mirror_path_for_worktree(worktree_path)
    if mirror is None:
        targets = [_ChownTarget(worktree_path, recursive=True)]
        if linked_git_dir is None:
            linked_git_dir = linked_worktree_git_dir(worktree_path)
        if linked_git_dir is not None:
            targets.append(_ChownTarget(linked_git_dir, recursive=True))
    else:
        targets = list(
            _agent_writable_git_targets(
                layout_mirror=mirror,
                worktree_path=worktree_path,
                linked_git_dir=linked_git_dir,
            )
        )
    _chown_targets(tuple(targets), uid, gid)


def mirror_path_for_worktree(worktree_path: Path) -> Path | None:
    """Return the bare mirror path backing a linked worktree, when discoverable."""
    linked_git_dir = linked_worktree_git_dir(worktree_path)
    if linked_git_dir is None:
        return None
    commondir = linked_git_dir / "commondir"
    if commondir.is_file():
        try:
            raw = commondir.read_text(encoding="utf-8").strip()
        except OSError:
            raw = ""
        if raw:
            common = Path(raw)
            if not common.is_absolute():
                common = linked_git_dir / common
            return common.resolve()
    return linked_git_dir.parent.parent.resolve()


def linked_worktree_git_dir(worktree_path: Path) -> Path | None:
    """Return the Git metadata directory linked from a worktree's ``.git`` file."""
    git_file = worktree_path / ".git"
    if not git_file.is_file():
        return None
    try:
        content = git_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    prefix = "gitdir: "
    if not content.startswith(prefix):
        return None
    git_dir = Path(content.removeprefix(prefix).strip())
    if not git_dir.is_absolute():
        git_dir = (worktree_path / git_dir).resolve()
    return git_dir


def _chown_targets(targets: tuple[_ChownTarget, ...], uid: int, gid: int) -> None:
    seen: set[tuple[Path, bool, bool]] = set()
    for target in targets:
        resolved = target.path.resolve()
        key = (resolved, target.recursive, target.directories_only)
        if key in seen or not (target.path.exists() or target.path.is_symlink()):
            continue
        seen.add(key)
        if target.recursive:
            _chown_tree(target.path, uid, gid, directories_only=target.directories_only)
        elif target.path.is_symlink():
            os.lchown(target.path, uid, gid)
        else:
            os.chown(target.path, uid, gid)


async def repair_mirror_hooks_path(mirror_path: Path) -> bool:
    """Clear a poisoned ``core.hooksPath`` from the shared bare mirror config.

    Returns ``True`` if repair was needed and succeeded, ``False`` if no repair
    was needed. Raises ``GitOperationError`` if the unset fails.
    """
    proc = await asyncio.create_subprocess_exec(
        "git",
        "--git-dir",
        str(mirror_path),
        "config",
        "--local",
        "core.hooksPath",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()
    assert proc.returncode is not None

    if proc.returncode != 0:
        return False

    unset = await asyncio.create_subprocess_exec(
        "git",
        "--git-dir",
        str(mirror_path),
        "config",
        "--unset",
        "core.hooksPath",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    unset_stdout_bytes, unset_stderr_bytes = await unset.communicate()
    unset_stdout = unset_stdout_bytes.decode("utf-8", errors="replace")
    unset_stderr = unset_stderr_bytes.decode("utf-8", errors="replace")
    assert unset.returncode is not None

    if unset.returncode != 0:
        raise GitOperationError(
            operation="mirror.hooks_path_repair",
            returncode=unset.returncode,
            stdout=unset_stdout,
            stderr=unset_stderr,
            reason_code="MIRROR_HOOKS_PATH_REPAIR_FAILED",
        )
    return True


async def verify_head_object_exists(worktree_path: Path) -> bool:
    """Return ``True`` when HEAD's commit object is reachable in the object database.

    Uses ``git cat-file -e HEAD^{commit}`` which exits 0 when the object exists
    and non-zero when the ref exists but the commit object is missing.
    """
    proc = await asyncio.create_subprocess_exec(
        "git",
        *git_safe_directory_config_args(worktree_path),
        "-C",
        str(worktree_path),
        "cat-file",
        "-e",
        "HEAD^{commit}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()
    assert proc.returncode is not None
    return proc.returncode == 0


def _chown_tree(path: Path, uid: int, gid: int, *, directories_only: bool = False) -> None:
    if path.is_symlink():
        os.lchown(path, uid, gid)
        return

    os.chown(path, uid, gid)
    if not path.is_dir():
        return

    for root, dirs, files in os.walk(path, followlinks=False):
        for name in dirs:
            child = Path(root) / name
            if child.is_symlink():
                os.lchown(child, uid, gid)
            else:
                os.chown(child, uid, gid)
        if directories_only:
            continue
        for name in files:
            child = Path(root) / name
            if child.is_symlink():
                os.lchown(child, uid, gid)
            else:
                os.chown(child, uid, gid)
