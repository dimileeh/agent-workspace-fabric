"""Filesystem ownership and Git environment helpers for worktree management."""

from __future__ import annotations

import os
import stat
from pathlib import Path

_GIT_OBJECT_LOOKUP_ENV_KEYS = ("GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES")
_GIT_BARE_REPOSITORY_PROBE_ENV_KEYS = (
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_CONFIG",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_PARAMETERS",
    "GIT_DIR",
    "GIT_GRAFT_FILE",
    "GIT_IMPLICIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_INTERNAL_SUPER_PREFIX",
    "GIT_NO_REPLACE_OBJECTS",
    "GIT_OBJECT_DIRECTORY",
    "GIT_PREFIX",
    "GIT_REPLACE_REF_BASE",
    "GIT_SHALLOW_FILE",
    "GIT_WORK_TREE",
)
_OWNER_WRITABLE_DIR_MODE = stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR


def git_env_without_object_lookup_overrides() -> dict[str, str]:
    """Return a copy of ``os.environ`` without Git object-lookup override variables."""
    env = dict(os.environ)
    for key in _GIT_OBJECT_LOOKUP_ENV_KEYS:
        env.pop(key, None)
    return env


def git_env_for_bare_repository_probe() -> dict[str, str]:
    """Build a Git environment suitable for bare-repository probe subprocesses."""
    env = git_env_without_object_lookup_overrides()
    for key in _GIT_BARE_REPOSITORY_PROBE_ENV_KEYS:
        env.pop(key, None)
    return env


def _chown_tree(path: Path, uid: int, gid: int, *, directories_only: bool = False) -> None:
    """Recursively chown a directory tree, honoring symlinks and optional file skipping."""
    if path.is_symlink():
        os.lchown(path, uid, gid)
        return

    os.chown(path, uid, gid)
    if not path.is_dir():
        return
    _ensure_owner_writable_dir(path)

    for root, dirs, files in os.walk(path, followlinks=False):
        for name in dirs:
            child = Path(root) / name
            if child.is_symlink():
                os.lchown(child, uid, gid)
            else:
                os.chown(child, uid, gid)
                _ensure_owner_writable_dir(child)
        if directories_only:
            continue
        for name in files:
            child = Path(root) / name
            if child.is_symlink():
                os.lchown(child, uid, gid)
            else:
                os.chown(child, uid, gid)


def _ensure_owner_writable_dir(path: Path) -> None:
    """Ensure ``path`` is owner-writable without changing unrelated permission bits."""
    mode = path.stat(follow_symlinks=False).st_mode
    desired_mode = stat.S_IMODE(mode | _OWNER_WRITABLE_DIR_MODE)
    if desired_mode != stat.S_IMODE(mode):
        path.chmod(desired_mode)
