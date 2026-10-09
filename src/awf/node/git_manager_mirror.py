"""Bare-mirror naming and ref-storage hardening helpers.

Naming (``_slugify_repo``, ``_checkout_tracking_ref``) is split out of
``git_manager`` so the module stays under the first-party line guardrail; it is
re-exported there for callers.

Ref-storage hardening exists because the bare mirror is **shared**: the
control plane writes its refs as root while an agent (uid 1000) holds a linked
worktree on the same mirror. A point-in-time chown of ``refs/``/``logs/`` loses
that race by construction (#1033):

1. root-side ``git gc --auto`` runs ``pack-refs``, which moves loose refs into
   ``packed-refs`` (root:root) and prunes the emptied loose-ref directories;
2. the next root ref write in that namespace — a base-sync merge commit, a
   host-side item commit, ``git worktree add -b <ns>/…`` for a sibling
   workspace — recreates the directory ``755 root:root``;
3. the agent then fails ``git commit`` with ``EACCES`` creating
   ``refs/heads/<ns>/<ws>.lock``.

So the mirror gets two durable defences on top of the chown baseline:
``gc.packRefs=false`` (step 1 never happens) and a **default POSIX ACL** on the
``refs/``/``logs/`` directory trees (directories root creates in step 2 inherit
agent write access), applied through descriptors the walk pins with
``O_NOFOLLOW`` so an agent cannot redirect the root-side ``setfacl`` out of the
mirror with a symlink. Both are best effort and reason-coded: a mirror that
cannot be hardened keeps working with the pre-#1033 race window, which the
post-ref-write ownership repair still covers.

The object database is deliberately never touched here — loose object files can
be immutable through Docker Desktop on macOS (``aa866959``).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Awaitable, Callable, Generator, Iterator
from functools import lru_cache
from pathlib import Path

from awf.common.logging import get_logger
from awf.common.redaction import redact_secrets

_log = get_logger(__name__)

_SLUG_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_GITHUB_PULL_HEAD_REF = re.compile(r"^refs/pull/([1-9][0-9]*)/head$")

# Local config applied to every AWF-managed bare mirror. ``gc.packRefs=false``
# is the narrow lever: ``git gc --auto`` then skips ``pack-refs``, so no loose
# ref directory is emptied and pruned under a running agent, while object
# repacking, loose-object pruning and reflog expiry keep working on these
# long-lived, constantly-fetched mirrors. Adding ``("gc.auto", "0")`` here would
# stop housekeeping altogether and trade the race for unbounded mirror growth.
MIRROR_REF_STORAGE_CONFIG_ENTRIES: tuple[tuple[str, str], ...] = (("gc.packRefs", "false"),)
# Mirror children whose directory trees get the inheritable agent ACL. Ref
# writes touch ``refs/`` and the matching reflog under ``logs/``; nothing else
# in a bare mirror is created by root mid-attempt in a namespace the agent
# needs to lock.
MIRROR_REF_DEFAULT_ACL_CHILDREN: tuple[str, ...] = ("refs", "logs")

MIRROR_REF_PACKING_CONFIG_FAILED_REASON = "MIRROR_REF_PACKING_CONFIG_FAILED"
MIRROR_REF_DEFAULT_ACL_UNSUPPORTED_REASON = "MIRROR_REF_DEFAULT_ACL_UNSUPPORTED"
MIRROR_REF_DEFAULT_ACL_FAILED_REASON = "MIRROR_REF_DEFAULT_ACL_FAILED"

_SETFACL_TIMEOUT_SECONDS = 20.0
# ``setfacl`` takes the paths as argv, so batch them to stay clear of ARG_MAX on
# mirrors with many ref namespaces. The batch also bounds how many pinned
# descriptors are held open at once.
_SETFACL_PATH_BATCH = 64
# The mirror directory itself lives in root-owned control-plane layout, so it is
# opened by path. Everything below it is agent-writable, so each component is
# opened relative to its already pinned parent with ``O_NOFOLLOW``: that is what
# keeps a symlink — final component *or* ancestor — out of the walk.
_ACL_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
_ACL_CHILD_DIR_FLAGS = _ACL_DIR_FLAGS | os.O_NOFOLLOW

# ``GitManager._run``: raises ``GitOperationError`` on a non-zero exit.
_MirrorGitRunner = Callable[..., Awaitable[object]]


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


async def ensure_mirror_ref_packing_disabled(
    mirror_path: Path,
    run: _MirrorGitRunner,
) -> bool:
    """Stop root-side auto-gc from packing (and pruning) this mirror's refs.

    Applied when the mirror is created and again whenever it is re-ensured, so
    mirrors that predate #1033 are repaired on their next provision.

    Advisory, never raising: the mirror is still perfectly usable without the
    setting, so a failure is logged with its reason code and reported to the
    caller instead of failing a provision. Nothing is retried and nothing is
    swallowed.

    Unlike the ownership/ACL layers this is **not** gated on ``geteuid() == 0``:
    writing a mirror's own local config needs no privilege, and a non-root
    control plane whose uid differs from the agent's is precisely the host that
    still needs root-side ``pack-refs`` kept away from its loose refs.
    """
    # Late import: ``git_manager`` loads this module while defining its types.
    from awf.node.git_manager import GitOperationError

    for key, value in MIRROR_REF_STORAGE_CONFIG_ENTRIES:
        try:
            await run(
                ["git", "--git-dir", str(mirror_path), "config", key, value],
                operation="mirror.disable_ref_packing",
            )
        except (GitOperationError, OSError) as exc:
            _log.warning(
                "mirror.ref_packing_config_failed",
                mirror_path=str(mirror_path),
                config_key=key,
                config_value=value,
                reason_code=MIRROR_REF_PACKING_CONFIG_FAILED_REASON,
                error=redact_secrets(str(exc)),
            )
            return False
    return True


@lru_cache(maxsize=1)
def _setfacl_executable() -> str | None:
    """Locate ``setfacl`` once per process, warning once when it is absent.

    The ownership repair runs several times per attempt, so a per-call warning
    would flood the log on an image built without the ``acl`` package. Whether
    the binary exists cannot change under a running control plane, so one
    reason-coded warning per process is the whole signal there is.
    """
    setfacl = shutil.which("setfacl")
    if setfacl is None:
        _log.warning(
            "mirror.ref_default_acl_unsupported",
            reason_code=MIRROR_REF_DEFAULT_ACL_UNSUPPORTED_REASON,
        )
    return setfacl


def _open_mirror_dir(mirror_path: Path) -> int | None:
    """Pin the mirror directory, or ``None`` when it cannot be opened."""
    try:
        return os.open(mirror_path, _ACL_DIR_FLAGS)
    except OSError:
        return None


def _open_pinned_child_dir(name: str, parent_fd: int) -> int | None:
    """Pin ``name`` under an already pinned parent without following links.

    ``None`` covers every entry that must not be given the ACL: a regular file
    (``ENOTDIR``, and ``setfacl -d`` rejects files anyway), a symlink
    (``ELOOP`` from ``O_NOFOLLOW`` — its target may live outside the mirror),
    and an entry that disappeared or is unopenable mid-walk.
    """
    try:
        return os.open(name, _ACL_CHILD_DIR_FLAGS, dir_fd=parent_fd)
    except OSError:
        return None


def _list_pinned_dir(dir_fd: int) -> list[str]:
    """Names in an already pinned directory; empty when it cannot be read."""
    try:
        return os.listdir(dir_fd)
    except OSError:
        return []


def _walk_pinned_acl_dir_fds(mirror_path: Path) -> Generator[int, None, None]:
    """Yield one pinned descriptor per mirror ref/log directory needing the ACL.

    Every directory in the trees, not just the top-level child: the default
    ACL has to sit on ``refs/heads/`` for a root-recreated ``refs/heads/<ns>/``
    to inherit it (the recreated directory then carries a default ACL of its
    own).

    Descriptors rather than pathnames because these trees are agent-writable:
    a pathname can be re-pointed between the check and the root-side
    ``setfacl``, while a descriptor opened with ``O_NOFOLLOW`` relative to its
    already pinned parent still names the directory that was validated.
    Ownership of a yielded descriptor passes to the caller; descriptors still
    being traversed are closed here, including when the caller stops early.
    """
    mirror_fd = _open_mirror_dir(mirror_path)
    if mirror_fd is None:
        return
    stack: list[tuple[int, Iterator[str]]] = []
    try:
        for child in MIRROR_REF_DEFAULT_ACL_CHILDREN:
            root_fd = _open_pinned_child_dir(child, mirror_fd)
            if root_fd is None:
                continue
            stack.append((root_fd, iter(_list_pinned_dir(root_fd))))
            # Post-order, and iterative because ref-tree depth is agent-chosen:
            # a directory is handed over only once its own entries have been
            # opened, so the walk never needs that descriptor again.
            while stack:
                parent_fd, names = stack[-1]
                name = next(names, None)
                if name is None:
                    stack.pop()
                    yield parent_fd
                    continue
                child_fd = _open_pinned_child_dir(name, parent_fd)
                if child_fd is not None:
                    stack.append((child_fd, iter(_list_pinned_dir(child_fd))))
    finally:
        for pending_fd, _names in stack:
            os.close(pending_fd)
        os.close(mirror_fd)


def _close_pinned(dir_fds: list[int]) -> None:
    """Close and forget a batch of pinned descriptors."""
    for dir_fd in dir_fds:
        os.close(dir_fd)
    dir_fds.clear()


def apply_mirror_ref_default_acls(mirror_path: Path, uid: int) -> bool:
    """Grant ``uid`` an inheritable default ACL on the mirror's ref namespaces.

    Directories git creates later under ``refs/``/``logs/`` inherit
    ``u:<uid>:rwx``, so a root-side ref write that recreates a pruned
    ``refs/heads/<namespace>/`` after the per-attempt chown still leaves the
    agent able to take its ref lock (#1033). The recursive chown stays the
    baseline; this is the layer that survives the race.

    The ACL is applied through descriptors pinned by the walk, never through a
    pathname: the agent owns these directories, so a pathname handed to a
    root-side ``setfacl`` could be swapped for a symlink first and grant
    ``uid`` inheritable access outside the mirror.

    Best effort by design: ``setfacl`` may be missing from the control-plane
    image, or the filesystem may not support ACLs (Docker Desktop's macOS file
    sharing, a ``noacl`` mount). Then a reason-coded warning is logged and
    ``False`` returned — a missing ACL layer must not fail a provision.
    """
    setfacl = _setfacl_executable()
    if setfacl is None:
        return False

    applied = True
    batch: list[int] = []
    directories = _walk_pinned_acl_dir_fds(mirror_path)
    try:
        for dir_fd in directories:
            batch.append(dir_fd)
            if len(batch) < _SETFACL_PATH_BATCH:
                continue
            if not _set_default_acl(setfacl, uid=uid, mirror_path=mirror_path, dir_fds=batch):
                applied = False
            _close_pinned(batch)
        if batch and not _set_default_acl(setfacl, uid=uid, mirror_path=mirror_path, dir_fds=batch):
            applied = False
    finally:
        _close_pinned(batch)
        directories.close()
    return applied


def _set_default_acl(
    setfacl: str,
    *,
    uid: int,
    mirror_path: Path,
    dir_fds: list[int],
) -> bool:
    """Run one ``setfacl -d`` batch on pinned descriptors, never raising.

    The arguments are ``/proc/self/fd/<fd>`` magic links resolved in the
    child's own descriptor table — ``pass_fds`` hands the descriptors over with
    their numbers intact — so the ACL lands on the directory the walk pinned
    instead of on whatever the pathname resolves to at exec time.
    """
    argv = [setfacl, "-d", "-m", f"u:{uid}:rwx", *(f"/proc/self/fd/{fd}" for fd in dir_fds)]
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_SETFACL_TIMEOUT_SECONDS,
            check=False,
            pass_fds=tuple(dir_fds),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _log.warning(
            "mirror.ref_default_acl_failed",
            mirror_path=str(mirror_path),
            reason_code=MIRROR_REF_DEFAULT_ACL_FAILED_REASON,
            directory_count=len(dir_fds),
            error=redact_secrets(str(exc)),
        )
        return False
    if completed.returncode != 0:
        _log.warning(
            "mirror.ref_default_acl_failed",
            mirror_path=str(mirror_path),
            reason_code=MIRROR_REF_DEFAULT_ACL_FAILED_REASON,
            directory_count=len(dir_fds),
            returncode=completed.returncode,
            stderr=redact_secrets((completed.stderr or "").strip()[-400:]),
        )
        return False
    return True
