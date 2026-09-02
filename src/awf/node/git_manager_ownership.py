"""Filesystem ownership and Git environment helpers for worktree management."""

from __future__ import annotations

import contextlib
import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path

_GIT_INCLUDE_SECTION = re.compile(r"^\[include\]\s*$", re.IGNORECASE)
_GIT_INCLUDE_IF_SECTION = re.compile(r"^\[includeIf\b", re.IGNORECASE)
_GIT_CONFIG_SECTION = re.compile(r"^\[")
_GIT_CONFIG_PATH_KEY = re.compile(r"^path\s*=", re.IGNORECASE)

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
# Trusted-base profile snapshots must not honor replace refs, grafts, object
# alternates, or injected config that a prior agent could leave on a shared mirror.
_GIT_TRUSTED_BASE_MATERIALIZATION_STRIP_KEYS = (
    *_GIT_BARE_REPOSITORY_PROBE_ENV_KEYS,
    "GIT_ATTR_SOURCE",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_SYSTEM",
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


def git_env_for_trusted_base_materialization(
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a Git env that ignores replace refs, grafts, and config/object overrides.

    Used for immutable trusted-base profile snapshot rev-parse/fetch/checkout and
    raw blob verification so a poisoned shared mirror cannot rewrite trees or
    ``.awf/workspace.yml`` bytes under an unchanged commit SHA.
    """
    env = dict(base_env) if base_env is not None else dict(os.environ)
    for key in _GIT_TRUSTED_BASE_MATERIALIZATION_STRIP_KEYS:
        env.pop(key, None)
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    env["GIT_GRAFT_FILE"] = os.devnull
    env["GIT_ATTR_NOSYSTEM"] = "1"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    return env


# Explicit ``-c`` overrides paired with :func:`git_env_for_trusted_base_materialization`.
# ``core.attributesFile=/dev/null`` only clears the *external* attributes file; it does
# **not** disable committed ``.gitattributes``. Trusted-base materialization therefore
# uses ``worktree add --no-checkout`` plus raw-object writes so repository filter
# drivers never execute during snapshot publish.
TRUSTED_BASE_GIT_CONFIG_ARGS: tuple[str, ...] = (
    "-c",
    f"core.attributesFile={os.devnull}",
    "-c",
    f"core.hooksPath={os.devnull}",
)

# Agent-controlled embedded repositories may ship a local ``.git/config`` with
# executable settings (``core.fsmonitor``, ``core.hooksPath``, …). Nested residue
# probes must override those via explicit ``-c`` flags (PRRT_kwDOSJAM6s6eV4s0).
# Committed ``.gitattributes`` filter drivers cannot be disabled statically here;
# nested probes use ``git diff-files`` / ``git ls-files -o`` instead of
# ``git diff`` / ``git status`` so clean filters never execute (PRRT_kwDOSJAM6s6eWICC).
# Staged probes still use ``git diff --cached --name-only``; disable lazy-fetch transports
# and external protocol helpers so missing promisor objects cannot execute ext:: remotes
# (PRRT_kwDOSJAM6s6eXXaD).
# Force ``core.fileMode=true``: with local ``core.fileMode=false``, ``diff-files``
# omits executable-bit flips so nested fingerprints collide (PRRT_kwDOSJAM6s6ekF15).
# ``-c`` cannot disable repository-local ``include.path`` / ``includeIf``: Git still
# opens and parses included files during every command. Nested probes must textually
# reject local includes before invoking Git (PRRT_kwDOSJAM6s6ekfTU).
UNTRUSTED_NESTED_GIT_CONFIG_ARGS: tuple[str, ...] = (
    *TRUSTED_BASE_GIT_CONFIG_ARGS,
    "-c",
    "core.fsmonitor=",
    "-c",
    "core.fileMode=true",
    "-c",
    "diff.external=",
    "-c",
    "diff.ignoreSubmodules=none",
    "-c",
    "protocol.allow=never",
    "-c",
    "protocol.ext.allow=never",
)


def git_env_for_untrusted_nested_repository_probe(
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a Git env for probing agent-controlled embedded repositories."""
    env = git_env_for_trusted_base_materialization(base_env)
    env["GIT_NO_LAZY_FETCH"] = "1"
    return env


def git_config_text_declares_includes(text: str) -> bool:
    """Return True when Git config text declares ``include`` / ``includeIf`` paths."""
    in_include_section = False
    for raw_line in text.splitlines():
        line = raw_line.split(";", 1)[0].split("#", 1)[0].strip()
        if not line:
            continue
        if _GIT_INCLUDE_SECTION.match(line) or _GIT_INCLUDE_IF_SECTION.match(line):
            in_include_section = True
            continue
        if _GIT_CONFIG_SECTION.match(line):
            in_include_section = False
            continue
        if in_include_section and _GIT_CONFIG_PATH_KEY.match(line):
            return True
    return False


def _read_git_dir_config_text(path: Path) -> str | None:
    """Return regular-file config text without following a final-component symlink."""
    try:
        mode = path.lstat().st_mode
    except OSError:
        return None
    if not stat.S_ISREG(mode):
        return None
    try:
        return path.read_text(encoding="utf-8", errors="surrogateescape")
    except OSError:
        return None


def _git_dir_local_config_paths(git_dir: Path) -> tuple[Path, ...]:
    return (git_dir / "config", git_dir / "config.worktree")


def _nested_repository_git_dirs_for_include_scan(nested_root: Path) -> tuple[Path, ...]:
    """Return git-dirs whose local config Git would load for ``nested_root`` probes."""
    marker = nested_root / ".git"
    try:
        marker_mode = marker.lstat().st_mode
    except OSError:
        return ()
    if stat.S_ISDIR(marker_mode):
        git_dir = marker
    elif stat.S_ISREG(marker_mode):
        text = _read_git_dir_config_text(marker)
        if text is None:
            return ()
        prefix = "gitdir:"
        if not text.startswith(prefix):
            return ()
        git_dir = Path(text[len(prefix) :].strip())
        if not git_dir.is_absolute():
            git_dir = nested_root / git_dir
        try:
            git_dir = git_dir.resolve()
        except OSError:
            return ()
    else:
        return ()

    dirs: list[Path] = [git_dir]
    common_text = _read_git_dir_config_text(git_dir / "commondir")
    if common_text is not None:
        common = Path(common_text.strip())
        if common.parts:
            if not common.is_absolute():
                common = git_dir / common
            with contextlib.suppress(OSError):
                dirs.append(common.resolve())
    return tuple(dirs)


def untrusted_nested_git_dir_declares_local_includes(git_dir: Path) -> bool:
    """Return True if ``git_dir`` local config declares include/includeIf paths."""
    for config_path in _git_dir_local_config_paths(git_dir):
        try:
            mode = config_path.lstat().st_mode
        except OSError:
            continue
        # Symlinked config is followed by Git; fail closed rather than missing includes.
        if stat.S_ISLNK(mode):
            return True
        if not stat.S_ISREG(mode):
            continue
        text = _read_git_dir_config_text(config_path)
        if text is None:
            continue
        if git_config_text_declares_includes(text):
            return True
    return False


def untrusted_nested_repository_local_config_has_includes(nested_root: Path) -> bool:
    """Return True when an embedded repo's local config declares includes.

    Repository-local ``include.path`` / ``includeIf`` still load during nested
    probes despite ``UNTRUSTED_NESTED_GIT_CONFIG_ARGS``; callers must fail closed
    before invoking Git (PRRT_kwDOSJAM6s6ekfTU).
    """
    for git_dir in _nested_repository_git_dirs_for_include_scan(nested_root):
        if untrusted_nested_git_dir_declares_local_includes(git_dir):
            return True
    return False


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
