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
agent write access). Both are best effort and reason-coded: a mirror that
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
from collections.abc import Awaitable, Callable
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
# mirrors with many ref namespaces.
_SETFACL_PATH_BATCH = 64

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


def _mirror_default_acl_directories(mirror_path: Path) -> tuple[Path, ...]:
    """Enumerate the mirror ref/log directories that need the default ACL.

    Every directory in the tree, not just the top-level child: the default ACL
    has to sit on ``refs/heads/`` for a root-recreated ``refs/heads/<ns>/`` to
    inherit it (the recreated directory then inherits a default ACL of its own).
    Regular files are excluded — ``setfacl -d`` rejects them — and so are
    symlinks, whose targets may live outside the mirror.
    """
    directories: list[Path] = []
    for child in MIRROR_REF_DEFAULT_ACL_CHILDREN:
        root = mirror_path / child
        if root.is_symlink() or not root.is_dir():
            continue
        # ``os.walk`` yields each directory once, so the collected set needs no
        # deduplication: ``root`` plus every nested directory name.
        directories.append(root)
        for walk_root, dir_names, _files in os.walk(root, followlinks=False):
            for name in dir_names:
                candidate = Path(walk_root) / name
                # A symlinked ref directory can point outside the mirror and
                # ``setfacl`` would follow it, so grant nothing through it.
                if not candidate.is_symlink():
                    directories.append(candidate)
    return tuple(directories)


def apply_mirror_ref_default_acls(mirror_path: Path, uid: int) -> bool:
    """Grant ``uid`` an inheritable default ACL on the mirror's ref namespaces.

    Directories git creates later under ``refs/``/``logs/`` inherit
    ``u:<uid>:rwx``, so a root-side ref write that recreates a pruned
    ``refs/heads/<namespace>/`` after the per-attempt chown still leaves the
    agent able to take its ref lock (#1033). The recursive chown stays the
    baseline; this is the layer that survives the race.

    Best effort by design: ``setfacl`` may be missing from the control-plane
    image, or the filesystem may not support ACLs (Docker Desktop's macOS file
    sharing, a ``noacl`` mount). Then a reason-coded warning is logged and
    ``False`` returned — a missing ACL layer must not fail a provision.
    """
    setfacl = _setfacl_executable()
    if setfacl is None:
        return False

    directories = _mirror_default_acl_directories(mirror_path)
    applied = True
    for start in range(0, len(directories), _SETFACL_PATH_BATCH):
        batch = directories[start : start + _SETFACL_PATH_BATCH]
        if not _set_default_acl(setfacl, uid=uid, mirror_path=mirror_path, paths=batch):
            applied = False
    return applied


def _set_default_acl(
    setfacl: str,
    *,
    uid: int,
    mirror_path: Path,
    paths: tuple[Path, ...],
) -> bool:
    """Run one ``setfacl -d`` batch, reporting failures without raising."""
    argv = [setfacl, "-d", "-m", f"u:{uid}:rwx", *(str(path) for path in paths)]
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_SETFACL_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _log.warning(
            "mirror.ref_default_acl_failed",
            mirror_path=str(mirror_path),
            reason_code=MIRROR_REF_DEFAULT_ACL_FAILED_REASON,
            directory_count=len(paths),
            error=redact_secrets(str(exc)),
        )
        return False
    if completed.returncode != 0:
        _log.warning(
            "mirror.ref_default_acl_failed",
            mirror_path=str(mirror_path),
            reason_code=MIRROR_REF_DEFAULT_ACL_FAILED_REASON,
            directory_count=len(paths),
            returncode=completed.returncode,
            stderr=redact_secrets((completed.stderr or "").strip()[-400:]),
        )
        return False
    return True
