"""Filesystem ownership and Git environment helpers for worktree management."""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import shutil
import stat
import struct
import tempfile
import time
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

# Git accepts same-line assignments after a section header
# (``[include] path = …``, ``[core] worktree = …``). Capture the optional
# remainder so include detection and core.worktree rewrite stay aligned
# (PRRT_kwDOSJAM6s6etk6T).
_GIT_INCLUDE_SECTION = re.compile(r"^\[include\](.*)$", re.IGNORECASE)
# ``includeIf`` subsection names may contain ``]`` inside quotes; match the
# header prefix only and locate the closing bracket with quote/escape awareness
# (PRRT_kwDOSJAM6s6evMAg).
_GIT_INCLUDE_IF_SECTION_PREFIX = re.compile(r"^\[includeIf\b", re.IGNORECASE)
_GIT_CONFIG_SECTION = re.compile(r"^\[")
_GIT_CONFIG_PATH_KEY = re.compile(r"^path\s*=", re.IGNORECASE)
_GIT_CORE_SECTION = re.compile(r"^\[core\](.*)$", re.IGNORECASE)
_GIT_ANY_SECTION_HEADER = re.compile(r"^\[([^\]]+)\](.*)$")
_GIT_CORE_WORKTREE_LINE = re.compile(
    r"^([ \t]*worktree[ \t]*=[ \t]*)(.*?)([ \t]*)$",
    re.IGNORECASE,
)
# ``git update-index --split-index`` writes ``sharedindex.<oid>`` beside ``index``.
_GIT_SHARED_INDEX_NAME = re.compile(r"^sharedindex\.[0-9a-fA-F]+$")
# Split-index ``index`` files are small deltas; bound the read used to resolve
# the single ``link``-extension OID (PRRT_kwDOSJAM6s6epUot).
_GIT_SPLIT_INDEX_MAX_BYTES = 256 * 1024

# Nested ``.git/config`` / gitfile / commondir reads are agent-controlled. Cap
# size and wall time, and open with ``O_NOFOLLOW|O_NONBLOCK`` so a post-lstat
# FIFO swap or never-EOF appender cannot hang or OOM the monitor
# (PRRT_kwDOSJAM6s6elA2N).
_GIT_DIR_CONFIG_OPEN_FLAGS = (
    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
)
_GIT_DIR_CONFIG_MAX_BYTES = 256 * 1024
_GIT_DIR_CONFIG_READ_CHUNK_BYTES = 64 * 1024
_GIT_DIR_CONFIG_READ_BUDGET_SECONDS = 2.0

# Agent-controlled object stores can plant path floods under ``.git/objects``.
# Stream enumeration under the same aggregate scale as worktree directory enum
# so materialization cannot buffer unbounded names or overrun the nested-probe
# scan budget (PRRT_kwDOSJAM6s6eq1r7). Depth must match worktree enum too: a
# deep attacker-controlled tree raises RecursionError otherwise (Bugbot
# 5094985052).
_OBJECT_STORE_ENUM_AGGREGATE_MAX_ENTRIES = 100_000
_OBJECT_STORE_ENUM_MAX_DEPTH = 256
_OBJECT_STORE_ENUM_BUDGET_SECONDS = 30.0
# Nested probe object/ref leaves are copied through the validated fd so staging
# does not retain one descriptor per leaf until probes finish (PRRT_kwDOSJAM6s6eteRs).
_OBJECT_STORE_LEAF_COPY_MAX_BYTES = 64 * 1024 * 1024
_OBJECT_STORE_LEAF_COPY_BUDGET_SECONDS = 30.0
_OBJECT_STORE_LEAF_COPY_CHUNK_BYTES = 64 * 1024


class _ObjectStoreEnumBudget:
    """Mutable aggregate entry + depth + deadline budget for object-store walks."""

    __slots__ = ("entries_remaining", "deadline", "max_depth")

    def __init__(self, *, entries_remaining: int, deadline: float, max_depth: int) -> None:
        self.entries_remaining = entries_remaining
        self.deadline = deadline
        self.max_depth = max_depth


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
# Force ``core.symlinks=true`` the same way: with local ``core.symlinks=false``,
# ``diff-files`` can treat symlink→file typechanges with identical link text as
# unchanged (review 5093517929).
# ``ls-files -o --exclude-standard`` honors ``core.excludesFile``; clear it so a
# foreign workspace/host exclude file cannot hide untracked residue
# (PRRT_kwDOSJAM6s6elh7f). Repository-local ``info/exclude`` is not cleared by
# ``-c``; nested probe snapshots must omit the live ``info`` link instead
# (PRRT_kwDOSJAM6s6enFGg).
# ``-c`` cannot disable repository-local ``include.path`` / ``includeIf``: Git still
# opens and parses included files during every command. Nested probes must textually
# reject local includes before invoking Git (PRRT_kwDOSJAM6s6ekfTU).
# Staged ``git diff --cached --name-only`` still honors ``diff.orderFile``; force
# ``/dev/null`` so an agent FIFO or foreign-workspace path cannot hang the probe
# or leak cross-workspace contents into residue attribution (PRRT_kwDOSJAM6s6esEnZ).
UNTRUSTED_NESTED_GIT_CONFIG_ARGS: tuple[str, ...] = (
    *TRUSTED_BASE_GIT_CONFIG_ARGS,
    "-c",
    f"core.excludesFile={os.devnull}",
    "-c",
    "core.fsmonitor=",
    "-c",
    "core.fileMode=true",
    "-c",
    "core.symlinks=true",
    "-c",
    "diff.external=",
    "-c",
    f"diff.orderFile={os.devnull}",
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


def _strip_git_config_line_comment(raw_line: str) -> str:
    """Strip a Git config line comment, honoring ``#`` / ``;`` inside quotes.

    Git starts comments with ``#`` or ``;`` only outside double-quoted spans
    (with backslash escapes inside quotes). Naive ``split('#')`` truncates a
    valid header such as ``[includeIf "onbranch:#foo"]`` and misses the
    following ``path`` (PRRT_kwDOSJAM6s6eutWw).
    """
    out: list[str] = []
    in_quote = False
    i = 0
    while i < len(raw_line):
        ch = raw_line[i]
        if in_quote:
            out.append(ch)
            if ch == "\\":
                if i + 1 < len(raw_line):
                    out.append(raw_line[i + 1])
                    i += 2
                    continue
                break
            if ch == '"':
                in_quote = False
            i += 1
            continue
        if ch in "#;":
            break
        if ch == '"':
            in_quote = True
        out.append(ch)
        i += 1
    return "".join(out).strip()


def _git_config_section_remainder_after_closing_bracket(line: str) -> str | None:
    """Return text after a quote/escape-aware section-closing ``]``, or ``None``.

    Git allows ``]`` inside double-quoted subsection names (for example
    ``[includeIf "onbranch:x]y"]``). A naive ``[^\\]]*`` / ``str.find(']')``
    stops early so same-line ``path =`` after the real closer is missed
    (PRRT_kwDOSJAM6s6evMAg).
    """
    if not line.startswith("["):
        return None
    in_quote = False
    i = 1
    while i < len(line):
        ch = line[i]
        if in_quote:
            if ch == "\\":
                i += 2 if i + 1 < len(line) else 1
                continue
            if ch == '"':
                in_quote = False
            i += 1
            continue
        if ch == '"':
            in_quote = True
            i += 1
            continue
        if ch == "]":
            return line[i + 1 :]
        i += 1
    return None


def git_config_text_declares_includes(text: str) -> bool:
    """Return True when Git config text declares ``include`` / ``includeIf`` paths."""
    # Git accepts a UTF-8 BOM on config files; keep scanning aligned so a BOM
    # attached to ``[include]`` / ``[includeIf`` cannot bypass the guard
    # (PRRT_kwDOSJAM6s6elA2I).
    if text.startswith("\ufeff"):
        text = text[1:]
    in_include_section = False
    for raw_line in text.splitlines():
        line = _strip_git_config_line_comment(raw_line)
        if not line:
            continue
        include_match = _GIT_INCLUDE_SECTION.match(line)
        include_if_remainder: str | None = None
        if include_match is None and _GIT_INCLUDE_IF_SECTION_PREFIX.match(line):
            include_if_remainder = _git_config_section_remainder_after_closing_bracket(line)
        if include_match is not None:
            in_include_section = True
            # Same-line ``path =`` after ``[include]`` / ``[includeIf …]``
            # (PRRT_kwDOSJAM6s6etk6T).
            remainder = include_match.group(1).strip()
            if remainder and _GIT_CONFIG_PATH_KEY.match(remainder):
                return True
            continue
        if include_if_remainder is not None:
            in_include_section = True
            remainder = include_if_remainder.strip()
            if remainder and _GIT_CONFIG_PATH_KEY.match(remainder):
                return True
            continue
        if _GIT_CONFIG_SECTION.match(line):
            in_include_section = False
            continue
        if in_include_section and _GIT_CONFIG_PATH_KEY.match(line):
            return True
    return False


def _read_git_dir_config_text(path: Path) -> str | None:
    """Return a size/deadline-bounded regular-file snapshot, or ``None``.

    Opens with ``O_NOFOLLOW|O_NONBLOCK``, re-validates the opened inode via
    ``fstat``, reads only the open-time ``st_size`` under a fixed byte/deadline
    cap, and re-``fstat``s so a concurrent appender or post-``lstat`` FIFO swap
    cannot hang or OOM the monitor (PRRT_kwDOSJAM6s6elA2N).
    """
    try:
        fd = os.open(path, _GIT_DIR_CONFIG_OPEN_FLAGS)
    except OSError:
        return None
    try:
        try:
            st = os.fstat(fd)
        except OSError:
            return None
        if not stat.S_ISREG(st.st_mode):
            return None
        if st.st_size < 0 or st.st_size > _GIT_DIR_CONFIG_MAX_BYTES:
            return None
        deadline = time.monotonic() + _GIT_DIR_CONFIG_READ_BUDGET_SECONDS
        remaining = st.st_size
        chunks: list[bytes] = []
        while remaining > 0:
            if time.monotonic() >= deadline:
                return None
            try:
                chunk = os.read(fd, min(_GIT_DIR_CONFIG_READ_CHUNK_BYTES, remaining))
            except OSError:
                return None
            if not chunk:
                return None
            chunks.append(chunk)
            remaining -= len(chunk)
        try:
            st_after = os.fstat(fd)
        except OSError:
            return None
        if not (
            stat.S_ISREG(st_after.st_mode)
            and st_after.st_size == st.st_size
            and st_after.st_ino == st.st_ino
            and st_after.st_dev == st.st_dev
            and st_after.st_mtime_ns == st.st_mtime_ns
            and st_after.st_ctime_ns == st.st_ctime_ns
        ):
            return None
        return b"".join(chunks).decode("utf-8", errors="surrogateescape")
    finally:
        os.close(fd)


def _git_dir_local_config_paths(git_dir: Path) -> tuple[Path, ...]:
    return (git_dir / "config", git_dir / "config.worktree")


def _resolved_git_metadata_within_roots(
    path: Path,
    roots: Sequence[Path],
) -> Path | None:
    """Return ``path`` resolved when it stays under one of ``roots``, else ``None``."""
    try:
        resolved = path.resolve()
    except OSError:
        return None
    for root in roots:
        try:
            resolved_root = root.resolve()
        except OSError:
            continue
        try:
            if resolved.is_relative_to(resolved_root):
                return resolved
        except (OSError, ValueError):
            continue
    return None


def _nested_git_metadata_containment_roots(
    nested_root: Path,
    containment_roots: Sequence[Path] | None,
) -> tuple[Path, ...] | None:
    """Return roots that may host nested gitfile / commondir metadata."""
    if containment_roots:
        return tuple(containment_roots)
    try:
        return (nested_root.resolve(),)
    except OSError:
        return None


def _nested_repository_git_dirs_for_include_scan(
    nested_root: Path,
    *,
    containment_roots: Sequence[Path] | None = None,
) -> tuple[Path, ...] | None:
    """Return git-dirs whose local config Git would load for ``nested_root`` probes.

    Returns ``None`` to fail closed when a regular gitfile/commondir cannot be
    snapshotted safely (PRRT_kwDOSJAM6s6elA2N), including when gitfile or
    commondir targets escape the approved workspace roots.
    """
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
            return None
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

    roots = _nested_git_metadata_containment_roots(nested_root, containment_roots)
    if roots is None:
        return None
    contained_git_dir = _resolved_git_metadata_within_roots(git_dir, roots)
    if contained_git_dir is None:
        return None
    git_dir = contained_git_dir

    dirs: list[Path] = [git_dir]
    common_path = git_dir / "commondir"
    try:
        common_mode = common_path.lstat().st_mode
    except OSError:
        return tuple(dirs)
    if stat.S_ISLNK(common_mode):
        return None
    if not stat.S_ISREG(common_mode):
        return tuple(dirs)
    common_text = _read_git_dir_config_text(common_path)
    if common_text is None:
        return None
    common = Path(common_text.strip())
    if common.parts:
        if not common.is_absolute():
            common = git_dir / common
        contained_common = _resolved_git_metadata_within_roots(common, roots)
        if contained_common is None:
            return None
        dirs.append(contained_common)
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
            # Present regular file but unsafe/unstable/oversized snapshot.
            return True
        if git_config_text_declares_includes(text):
            return True
    return False


def _snapshot_git_dir_local_configs_via_fd(dir_fd: int) -> dict[str, str] | None:
    """Return validated local config snapshots via ``openat``, or ``None``."""
    out: dict[str, str] = {}
    for name in ("config", "config.worktree"):
        try:
            st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        except OSError:
            continue
        if stat.S_ISLNK(st.st_mode):
            return None
        if not stat.S_ISREG(st.st_mode):
            continue
        text = _read_git_dir_child_text_via_fd(dir_fd, name)
        if text is None:
            return None
        if git_config_text_declares_includes(text):
            return None
        out[name] = text
    return out


def untrusted_nested_repository_local_config_has_includes(
    nested_root: Path,
    *,
    containment_roots: Sequence[Path] | None = None,
) -> bool:
    """Return True when an embedded repo's local config declares includes.

    Repository-local ``include.path`` / ``includeIf`` still load during nested
    probes despite ``UNTRUSTED_NESTED_GIT_CONFIG_ARGS``; callers must fail closed
    before invoking Git (PRRT_kwDOSJAM6s6ekfTU). Gitfile and commondir targets
    must stay under ``containment_roots`` (default: ``nested_root``).
    """
    git_dirs = _nested_repository_git_dirs_for_include_scan(
        nested_root,
        containment_roots=containment_roots,
    )
    if git_dirs is None:
        return True
    return any(untrusted_nested_git_dir_declares_local_includes(git_dir) for git_dir in git_dirs)


def _snapshot_git_dir_local_configs(git_dir: Path) -> dict[str, str] | None:
    """Return validated local config snapshots, or ``None`` to fail closed."""
    out: dict[str, str] = {}
    for config_path in _git_dir_local_config_paths(git_dir):
        try:
            mode = config_path.lstat().st_mode
        except OSError:
            continue
        if stat.S_ISLNK(mode):
            return None
        if not stat.S_ISREG(mode):
            continue
        text = _read_git_dir_config_text(config_path)
        if text is None:
            return None
        if git_config_text_declares_includes(text):
            return None
        out[config_path.name] = text
    return out


def _copy_opened_regular_file_to_path(
    fd: int,
    dest: Path,
    *,
    max_bytes: int = _OBJECT_STORE_LEAF_COPY_MAX_BYTES,
    budget_seconds: float = _OBJECT_STORE_LEAF_COPY_BUDGET_SECONDS,
) -> bool:
    """Stream a size/deadline-bounded private copy from an opened regular file.

    Used for nested-probe staging leaves so callers can close ``fd`` immediately
    instead of retaining one descriptor per object/ref until probes finish
    (PRRT_kwDOSJAM6s6eteRs). Returns ``False`` on type/size/stability failures.
    """
    try:
        st = os.fstat(fd)
    except OSError:
        return False
    if not stat.S_ISREG(st.st_mode):
        return False
    if st.st_size < 0 or st.st_size > max_bytes:
        return False
    deadline = time.monotonic() + budget_seconds
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    try:
        out_fd = os.open(dest, flags, 0o644)
    except OSError:
        return False
    copied = 0
    succeeded = False
    try:
        remaining = st.st_size
        while remaining > 0:
            if time.monotonic() >= deadline:
                return False
            try:
                chunk = os.read(fd, min(_OBJECT_STORE_LEAF_COPY_CHUNK_BYTES, remaining))
            except OSError:
                return False
            if not chunk:
                return False
            view = memoryview(chunk)
            while view:
                if time.monotonic() >= deadline:
                    return False
                try:
                    written = os.write(out_fd, view)
                except OSError:
                    return False
                if written <= 0:
                    return False
                view = view[written:]
            copied += len(chunk)
            remaining -= len(chunk)
        try:
            st_after = os.fstat(fd)
        except OSError:
            return False
        if not (
            stat.S_ISREG(st_after.st_mode)
            and st_after.st_size == st.st_size
            and st_after.st_ino == st.st_ino
            and st_after.st_dev == st.st_dev
            and st_after.st_mtime_ns == st.st_mtime_ns
            and st_after.st_ctime_ns == st.st_ctime_ns
            and copied == st.st_size
        ):
            return False
        succeeded = True
        return True
    finally:
        os.close(out_fd)
        if not succeeded:
            with contextlib.suppress(OSError):
                dest.unlink()


def _symlink_git_dir_child_via_fd(
    dir_fd: int,
    name: str,
    dest: Path,
    held_fds: list[int],
    *,
    expect_directory: bool | None = None,
) -> bool:
    """Materialize ``dest`` from the opened inode of ``name`` under ``dir_fd``.

    Absolute pathnames into ``.git/...`` break under a post-open rename of the
    git-dir (attacker plants a symlink at the old path). Directory pins still
    use ``/proc/<pid>/fd/<child_fd>``. Regular-file leaves are copied through the
    validated child fd into a private staging file so inode bytes stay pinned
    against a post-validation name swap (PRRT_kwDOSJAM6s6ercEO) without retaining
    one descriptor per object/ref leaf for the probe lifetime
    (PRRT_kwDOSJAM6s6eteRs).

    Callers must keep every appended ``held_fds`` entry (directory pins only)
    open until staging is discarded, then close them.

    Returns ``False`` when ``name`` is present but unsafe (symlink, wrong type,
    or unreadable) so callers fail closed. Missing names return ``True``
    (nothing to link). Symlinked ``refs`` / ``packed-refs`` / ``index`` would
    otherwise chain through the staging link into a foreign workspace
    (PRRT_kwDOSJAM6s6eqQgm).
    """
    try:
        st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return False
    if expect_directory is True and not stat.S_ISDIR(st.st_mode):
        return False
    if expect_directory is False and not stat.S_ISREG(st.st_mode):
        return False

    want_directory = expect_directory is True or (
        expect_directory is None and stat.S_ISDIR(st.st_mode)
    )
    if want_directory:
        child_fd = _open_git_dir_child_directory_fd(dir_fd, name)
        if child_fd is None:
            return False
        try:
            dest.symlink_to(f"/proc/{os.getpid()}/fd/{child_fd}")
        except OSError:
            os.close(child_fd)
            return False
        held_fds.append(child_fd)
        return True

    try:
        child_fd = os.open(name, _GIT_DIR_CONFIG_OPEN_FLAGS, dir_fd=dir_fd)
    except OSError:
        return False
    try:
        if not _copy_opened_regular_file_to_path(child_fd, dest):
            return False
    finally:
        os.close(child_fd)
    return True


def _read_fd_regular_file_bytes(fd: int, *, max_bytes: int) -> bytes | None:
    """Return a size/deadline-bounded snapshot of an already-opened regular file."""
    try:
        st = os.fstat(fd)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    if st.st_size < 0 or st.st_size > max_bytes:
        return None
    deadline = time.monotonic() + _GIT_DIR_CONFIG_READ_BUDGET_SECONDS
    remaining = st.st_size
    chunks: list[bytes] = []
    while remaining > 0:
        if time.monotonic() >= deadline:
            return None
        try:
            chunk = os.read(fd, min(_GIT_DIR_CONFIG_READ_CHUNK_BYTES, remaining))
        except OSError:
            return None
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    try:
        st_after = os.fstat(fd)
    except OSError:
        return None
    if not (
        stat.S_ISREG(st_after.st_mode)
        and st_after.st_size == st.st_size
        and st_after.st_ino == st.st_ino
        and st_after.st_dev == st.st_dev
        and st_after.st_mtime_ns == st.st_mtime_ns
        and st_after.st_ctime_ns == st.st_ctime_ns
    ):
        return None
    return b"".join(chunks)


def _read_git_dir_child_bytes_via_fd(dir_fd: int, name: str, *, max_bytes: int) -> bytes | None:
    """Bounded ``openat`` read of a git-dir child; ``None`` fails closed."""
    try:
        fd = os.open(name, _GIT_DIR_CONFIG_OPEN_FLAGS, dir_fd=dir_fd)
    except OSError:
        return None
    try:
        return _read_fd_regular_file_bytes(fd, max_bytes=max_bytes)
    finally:
        os.close(fd)


def _read_git_dir_child_text_via_fd(dir_fd: int, name: str) -> str | None:
    """Bounded ``openat`` text snapshot of a git-dir child; ``None`` fails closed."""
    raw = _read_git_dir_child_bytes_via_fd(dir_fd, name, max_bytes=_GIT_DIR_CONFIG_MAX_BYTES)
    if raw is None:
        return None
    return raw.decode("utf-8", errors="surrogateescape")


def _git_index_hash_len(data: bytes) -> int | None:
    """Return trailing checksum length when it matches SHA-1 or SHA-256."""
    for hash_len, factory in ((20, hashlib.sha1), (32, hashlib.sha256)):
        if len(data) < 12 + hash_len:
            continue
        if factory(data[:-hash_len]).digest() == data[-hash_len:]:
            return hash_len
    return None


def _decode_git_index_varint(data: bytes, pos: int) -> tuple[int, int] | None:
    """Decode one Git index unsigned varint; ``None`` on truncate/overflow."""
    if pos >= len(data):
        return None
    c = data[pos]
    pos += 1
    val = c & 0x7F
    while c & 0x80:
        val += 1
        # git ``decode_varint`` MSB(val, 7) guard (64-bit). Python ints do not
        # wrap, so the C ``!val`` overflow check is unnecessary here.
        if (val >> 57) != 0:
            return None
        if pos >= len(data):
            return None
        c = data[pos]
        pos += 1
        val = (val << 7) + (c & 0x7F)
    return val, pos


def _skip_git_index_entries(
    data: bytes, *, entry_count: int, version: int, hash_len: int
) -> int | None:
    """Return byte offset after ``entry_count`` index entries, or ``None``."""
    pos = 12
    body_end = len(data) - hash_len
    prev_path = b""
    for _ in range(entry_count):
        # fixed: ctime(8) mtime(8) dev/ino/mode/uid/gid/size (6*4) + oid + flags(2)
        fixed = 40 + hash_len + 2
        if pos + fixed > body_end:
            return None
        flags = struct.unpack(">H", data[pos + 40 + hash_len : pos + fixed])[0]
        extended = bool(flags & 0x4000)
        entry_start = pos
        pos = pos + fixed
        if extended:
            if version < 3 or pos + 2 > body_end:
                return None
            pos += 2
        if version == 4:
            decoded = _decode_git_index_varint(data, pos)
            if decoded is None:
                return None
            strip, pos = decoded
            if strip > len(prev_path) or pos >= body_end:
                return None
            try:
                nul = data.index(b"\0", pos, body_end)
            except ValueError:
                return None
            suffix = data[pos:nul]
            pos = nul + 1
            prev_path = prev_path[: len(prev_path) - strip] + suffix
        else:
            namelen = flags & 0xFFF
            if namelen == 0xFFF:
                try:
                    nul = data.index(b"\0", pos, body_end)
                except ValueError:
                    return None
                pos = nul + 1
            else:
                pos += namelen + 1
                if pos > body_end:
                    return None
            pad = (8 - ((pos - entry_start) % 8)) % 8
            pos += pad
            if pos > body_end:
                return None
    return pos


def _split_index_shared_oid_hex(index_bytes: bytes) -> str | None:
    """Return ``sharedindex`` OID hex from a split-index ``link`` extension."""
    if len(index_bytes) < 12 or index_bytes[:4] != b"DIRC":
        return None
    version, entry_count = struct.unpack(">II", index_bytes[4:12])
    if version not in (2, 3, 4):
        return None
    hash_len = _git_index_hash_len(index_bytes)
    if hash_len is None:
        return None
    pos = _skip_git_index_entries(
        index_bytes, entry_count=entry_count, version=version, hash_len=hash_len
    )
    if pos is None:
        return None
    end = len(index_bytes) - hash_len
    while pos + 8 <= end:
        signature = index_bytes[pos : pos + 4]
        size = struct.unpack(">I", index_bytes[pos + 4 : pos + 8])[0]
        pos += 8
        if pos + size > end:
            return None
        body = index_bytes[pos : pos + size]
        pos += size
        if signature != b"link":
            continue
        if len(body) < hash_len:
            return None
        return body[:hash_len].hex()
    return None


def _symlink_split_index_backing_files_via_fd(
    dir_fd: int, staging: Path, held_fds: list[int]
) -> bool:
    """Link the single ``sharedindex.<oid>`` referenced by a split-index ``index``.

    Snapshotting only ``index`` omits the referenced shared-index backing file,
    so snapshot-scoped ``diff-files`` exits 128 with ``index file open failed``
    (PRRT_kwDOSJAM6s6eo3py). Resolve the OID from the ``link`` extension instead
    of enumerating every ``sharedindex.*`` name under the agent-controlled
    git-dir (PRRT_kwDOSJAM6s6epUot). Open the index through the held directory
    fd so a post-open rename cannot redirect the read.

    Returns ``False`` when a referenced ``sharedindex.<oid>`` is present but
    unsafe (symlink / non-regular), matching packed-refs/index rejection
    (PRRT_kwDOSJAM6s6eqQgm).
    """
    index_bytes = _read_git_dir_child_bytes_via_fd(
        dir_fd, "index", max_bytes=_GIT_SPLIT_INDEX_MAX_BYTES
    )
    if index_bytes is None:
        return True
    oid_hex = _split_index_shared_oid_hex(index_bytes)
    if oid_hex is None:
        return True
    name = f"sharedindex.{oid_hex}"
    if (
        _GIT_SHARED_INDEX_NAME.fullmatch(name) is None
    ):  # pragma: no cover - oid.hex() always matches
        return True
    return _symlink_git_dir_child_via_fd(
        dir_fd, name, staging / name, held_fds, expect_directory=False
    )


def _open_git_dir_directory_fd(git_dir: Path) -> int | None:
    """Open a git-dir as ``O_DIRECTORY|O_NOFOLLOW`` for stable snapshot links."""
    flags = (
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        fd = os.open(git_dir, flags)
    except OSError:
        return None
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            os.close(fd)
            return None
    except OSError:
        os.close(fd)
        return None
    return fd


def _open_git_dir_child_directory_fd(dir_fd: int, name: str) -> int | None:
    """Open a child directory via ``openat`` with ``O_NOFOLLOW``, or ``None``."""
    flags = (
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        fd = os.open(name, flags, dir_fd=dir_fd)
    except OSError:
        return None
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            os.close(fd)
            return None
    except OSError:
        os.close(fd)
        return None
    return fd


def _proc_self_fd_number(path: Path) -> int | None:
    """Return the fd number for a ``/proc/self/fd/<n>`` pin path, else ``None``."""
    parts = path.parts
    if (
        len(parts) == 5
        and parts[1] == "proc"
        and parts[2] == "self"
        and parts[3] == "fd"
        and parts[4].isdigit()
    ):
        return int(parts[4])
    return None


def _pinned_directory_path(dir_fd: int) -> Path:
    """Return the ``/proc/self/fd/<dir_fd>`` path for an opened directory."""
    return Path(f"/proc/self/fd/{dir_fd}")


def _open_nested_root_directory_fd(nested_root: Path) -> int | None:
    """Open ``nested_root`` without dropping a retained ``/proc/self/fd/<n>`` pin.

    ``O_NOFOLLOW`` refuses the proc symlink itself, so dup the already-open
    descriptor instead of reopening a resolved pathname (PRRT_kwDOSJAM6s6evMAl).
    """
    fd_no = _proc_self_fd_number(nested_root)
    if fd_no is None:
        return _open_git_dir_directory_fd(nested_root)
    try:
        fd = os.dup(fd_no)
    except OSError:
        return None
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            os.close(fd)
            return None
    except OSError:
        os.close(fd)
        return None
    return fd


def _open_relative_directory_from_dir_fd(dir_fd: int, relative: Path) -> int | None:
    """Walk ``relative`` from ``dir_fd`` with component-wise ``O_NOFOLLOW``."""
    if relative.is_absolute():
        return None
    try:
        current = os.dup(dir_fd)
    except OSError:
        return None
    try:
        for part in relative.parts:
            if part == ".":
                continue
            next_fd = _open_git_dir_child_directory_fd(current, part)
            os.close(current)
            current = -1
            if next_fd is None:
                return None
            current = next_fd
        owned = current
        current = -1
        return owned
    except OSError:  # pragma: no cover - os.close rarely raises after a successful openat
        if current >= 0:
            with contextlib.suppress(OSError):
                os.close(current)
        return None


def _open_contained_directory_nofollow(
    probe: Path,
    containment_roots: Sequence[Path],
) -> int | None:
    """Open ``probe`` by walking from a containing root with ``O_NOFOLLOW``."""
    try:
        resolved = probe.resolve()
    except OSError:
        return None
    for root in containment_roots:
        try:
            resolved_root = root.resolve()
            relative = resolved.relative_to(resolved_root)
        except (OSError, ValueError):
            continue
        root_fd = _open_git_dir_directory_fd(resolved_root)
        if root_fd is None:
            continue
        walked: int | None = None
        try:
            walked = _open_relative_directory_from_dir_fd(root_fd, relative)
        finally:
            os.close(root_fd)
        if walked is not None:
            return walked
    return None


def _open_git_metadata_candidate(
    candidate: Path,
    *,
    base_fd: int,
    containment_roots: Sequence[Path],
) -> int | None:
    """Open a gitfile/commondir target through retained fds, or ``None``."""
    if not candidate.parts:
        return None
    probe = candidate if candidate.is_absolute() else _pinned_directory_path(base_fd) / candidate
    if _resolved_git_metadata_within_roots(probe, containment_roots) is None:
        return None
    if not candidate.is_absolute():
        return _open_relative_directory_from_dir_fd(base_fd, candidate)
    try:
        relative = probe.resolve().relative_to(_pinned_directory_path(base_fd).resolve())
    except (OSError, ValueError):
        relative = None
    if relative is not None:
        return _open_relative_directory_from_dir_fd(base_fd, relative)
    return _open_contained_directory_nofollow(probe, containment_roots)


def _open_nested_probe_git_dir_fds(
    nested_fd: int,
    *,
    containment_roots: Sequence[Path],
) -> tuple[int, int] | None:
    """Return ``(primary_fd, object_fd)`` opened via ``openat`` / no-follow walks.

    ``object_fd`` is ``primary_fd`` when ``commondir`` is absent. The caller owns
    both descriptors and must close ``object_fd`` only when it differs.
    """
    try:
        marker_mode = os.stat(".git", dir_fd=nested_fd, follow_symlinks=False).st_mode
    except OSError:
        return None
    primary_fd: int | None
    if stat.S_ISDIR(marker_mode):
        primary_fd = _open_git_dir_child_directory_fd(nested_fd, ".git")
        if primary_fd is None:
            return None
        if (
            _resolved_git_metadata_within_roots(
                _pinned_directory_path(primary_fd),
                containment_roots,
            )
            is None
        ):
            os.close(primary_fd)
            return None
    elif stat.S_ISREG(marker_mode):
        text = _read_git_dir_child_text_via_fd(nested_fd, ".git")
        if text is None:
            return None
        prefix = "gitdir:"
        if not text.startswith(prefix):
            return None
        git_dir = Path(text[len(prefix) :].strip())
        if not git_dir.parts:
            return None
        primary_fd = _open_git_metadata_candidate(
            git_dir, base_fd=nested_fd, containment_roots=containment_roots
        )
        if primary_fd is None:
            return None
    else:
        return None

    try:
        common_mode = os.stat("commondir", dir_fd=primary_fd, follow_symlinks=False).st_mode
    except FileNotFoundError:
        return primary_fd, primary_fd
    except OSError:
        os.close(primary_fd)
        return None
    if stat.S_ISLNK(common_mode):
        os.close(primary_fd)
        return None
    if not stat.S_ISREG(common_mode):
        return primary_fd, primary_fd
    common_text = _read_git_dir_child_text_via_fd(primary_fd, "commondir")
    if common_text is None:
        os.close(primary_fd)
        return None
    common = Path(common_text.strip())
    if not common.parts:
        return primary_fd, primary_fd
    common_fd = _open_git_metadata_candidate(
        common, base_fd=primary_fd, containment_roots=containment_roots
    )
    if common_fd is None:
        os.close(primary_fd)
        return None
    return primary_fd, common_fd


def _git_dir_declares_object_alternates(object_fd: int) -> bool:
    """Return True when ``objects/info/alternates`` is present or unreadable.

    Nested probe snapshots omit live ``objects/info``, but an existing
    ``alternates`` file at check time often means objects already live only in a
    foreign store; fail closed early (PRRT_kwDOSJAM6s6ep1TL). Missing
    ``objects`` / ``info`` / ``alternates`` is fine; any other probe failure fails
    closed as declared.
    """
    try:
        os.stat("objects", dir_fd=object_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    objects_fd = _open_git_dir_child_directory_fd(object_fd, "objects")
    if objects_fd is None:
        return True
    try:
        try:
            os.stat("info", dir_fd=objects_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        except OSError:
            return True
        info_fd = _open_git_dir_child_directory_fd(objects_fd, "info")
        if info_fd is None:
            return True
        try:
            try:
                os.stat("alternates", dir_fd=info_fd, follow_symlinks=False)
            except FileNotFoundError:
                return False
            except OSError:
                return True
            return True
        finally:
            os.close(info_fd)
    finally:
        os.close(objects_fd)


def _symlink_object_store_tree_via_fd(
    dir_fd: int,
    staging_dir: Path,
    held_fds: list[int],
    *,
    skip_names: frozenset[str] = frozenset(),
    budget: _ObjectStoreEnumBudget | None = None,
    depth: int = 0,
) -> bool:
    """Materialize ``staging_dir`` from ``dir_fd`` without linking directory subtrees.

    Symlinking a whole fan-out or ``pack`` directory would approve nested
    loose-object / pack symlinks and expose them through the staging link; Git
    follows those symlinks when resolving objects (PRRT_kwDOSJAM6s6eq1r3).
    Create real staging directories and copy only non-symlink regular-file leaves
    through held child file fds (PRRT_kwDOSJAM6s6ercEO / PRRT_kwDOSJAM6s6eteRs).

    Enumeration streams via ``/proc/self/fd/<dir_fd>`` under a shared aggregate
    entry + depth + wall-time budget so a path flood cannot ``listdir``-buffer
    unbounded names, recurse past the worktree depth scale, or create staging
    links past the nested-probe scan window (PRRT_kwDOSJAM6s6eq1r7 /
    Bugbot 5094985052).
    """
    if budget is None:
        budget = _ObjectStoreEnumBudget(
            entries_remaining=_OBJECT_STORE_ENUM_AGGREGATE_MAX_ENTRIES,
            deadline=time.monotonic() + _OBJECT_STORE_ENUM_BUDGET_SECONDS,
            max_depth=_OBJECT_STORE_ENUM_MAX_DEPTH,
        )
    if depth > budget.max_depth:
        return False
    if time.monotonic() >= budget.deadline:
        return False
    try:
        # Path.iterdir cannot list an open directory fd; pin via ``/proc`` and
        # stream so caps apply before any full listing is buffered.
        with os.scandir(f"/proc/self/fd/{dir_fd}") as entries:
            for entry in entries:
                if entry.name in {".", ".."}:
                    continue
                if time.monotonic() >= budget.deadline:
                    return False
                if budget.entries_remaining <= 0:
                    return False
                budget.entries_remaining -= 1
                name = entry.name
                if name in skip_names:
                    continue
                try:
                    st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                except OSError:
                    return False
                if stat.S_ISLNK(st.st_mode):
                    return False
                if stat.S_ISREG(st.st_mode):
                    if not _symlink_git_dir_child_via_fd(
                        dir_fd,
                        name,
                        staging_dir / name,
                        held_fds,
                        expect_directory=False,
                    ):
                        return False
                    continue
                if not stat.S_ISDIR(st.st_mode):
                    return False
                child_fd = _open_git_dir_child_directory_fd(dir_fd, name)
                if child_fd is None:
                    return False
                child_staging = staging_dir / name
                try:
                    child_staging.mkdir()
                except OSError:
                    os.close(child_fd)
                    return False
                try:
                    if not _symlink_object_store_tree_via_fd(
                        child_fd,
                        child_staging,
                        held_fds,
                        budget=budget,
                        depth=depth + 1,
                    ):
                        return False
                finally:
                    # Directory fds are only needed for the walk; retaining them
                    # until probes finish is unnecessary once leaves are copied
                    # (PRRT_kwDOSJAM6s6eteRs).
                    os.close(child_fd)
    except OSError:
        return False
    return True


def _symlink_nested_probe_objects_store_via_fd(
    object_fd: int, staging: Path
) -> tuple[bool, list[int]]:
    """Materialize ``staging/objects`` without linking live ``objects/info``.

    Symlinking the whole live ``objects`` tree preserves ``info/alternates`` both
    at check time and for late creation after ``_git_dir_declares_object_alternates``
    (Bugbot 5094509768). Materialize store children via transient directory fds,
    skip ``info``, and never link whole fan-out directories so nested loose-object
    symlinks cannot reach snapshot probes (PRRT_kwDOSJAM6s6eq1r3). Regular-file
    leaves are private copies so descriptors are not retained for the probe
    lifetime (PRRT_kwDOSJAM6s6eteRs).

    Returns ``(ok, held_fds)``. Successful materialization returns an empty held
    list; callers may still close any returned fds defensively.
    """
    held_fds: list[int] = []

    def _close_held() -> None:
        for held in held_fds:
            with contextlib.suppress(OSError):
                os.close(held)
        held_fds.clear()

    try:
        os.stat("objects", dir_fd=object_fd, follow_symlinks=False)
    except FileNotFoundError:
        return True, []
    except OSError:
        return False, []
    objects_fd = _open_git_dir_child_directory_fd(object_fd, "objects")
    if objects_fd is None:
        return False, []
    held_fds.append(objects_fd)
    try:
        staging_objects = staging / "objects"
        try:
            staging_objects.mkdir()
        except OSError:
            _close_held()
            return False, []
        budget = _ObjectStoreEnumBudget(
            entries_remaining=_OBJECT_STORE_ENUM_AGGREGATE_MAX_ENTRIES,
            deadline=time.monotonic() + _OBJECT_STORE_ENUM_BUDGET_SECONDS,
            max_depth=_OBJECT_STORE_ENUM_MAX_DEPTH,
        )
        if not _symlink_object_store_tree_via_fd(
            objects_fd,
            staging_objects,
            held_fds,
            skip_names=frozenset({"info"}),
            budget=budget,
        ):
            _close_held()
            return False, []
        _close_held()
        return True, []
    except BaseException:
        _close_held()
        raise


def _symlink_nested_probe_refs_store_via_fd(
    object_fd: int, staging: Path
) -> tuple[bool, list[int]]:
    """Materialize ``staging/refs`` without linking whole live ref subtrees.

    Symlinking the live ``refs`` directory would approve nested loose-ref
    symlinks (e.g. ``refs/heads/main`` → foreign workspace) and expose them
    through the staging link; Git follows those symlinks when resolving HEAD
    (PRRT_kwDOSJAM6s6ercEL). Materialize ref directories via transient fds and copy
    only non-symlink regular-file leaves, matching the objects-store walk
    (PRRT_kwDOSJAM6s6eteRs).

    Returns ``(ok, held_fds)``. Successful materialization returns an empty held
    list; callers may still close any returned fds defensively.
    """
    held_fds: list[int] = []

    def _close_held() -> None:
        for held in held_fds:
            with contextlib.suppress(OSError):
                os.close(held)
        held_fds.clear()

    try:
        st = os.stat("refs", dir_fd=object_fd, follow_symlinks=False)
    except FileNotFoundError:
        return True, []
    except OSError:
        return False, []
    # Top-level ``refs`` must be a real directory; a symlink here is the same
    # foreign-store chain already rejected by the previous whole-tree link
    # (PRRT_kwDOSJAM6s6eqQgm).
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        return False, []
    refs_fd = _open_git_dir_child_directory_fd(object_fd, "refs")
    if refs_fd is None:
        return False, []
    held_fds.append(refs_fd)
    try:
        staging_refs = staging / "refs"
        try:
            staging_refs.mkdir()
        except OSError:
            _close_held()
            return False, []
        budget = _ObjectStoreEnumBudget(
            entries_remaining=_OBJECT_STORE_ENUM_AGGREGATE_MAX_ENTRIES,
            deadline=time.monotonic() + _OBJECT_STORE_ENUM_BUDGET_SECONDS,
            max_depth=_OBJECT_STORE_ENUM_MAX_DEPTH,
        )
        if not _symlink_object_store_tree_via_fd(
            refs_fd,
            staging_refs,
            held_fds,
            budget=budget,
        ):
            _close_held()
            return False, []
        _close_held()
        return True, []
    except BaseException:
        _close_held()
        raise


def _unquote_git_config_value(raw: str) -> str:
    """Decode a Git config value token, honoring quotes and trailing comments.

    Git allows ``worktree = "../rel" # note``. Only treating fully-quoted tokens
    as quoted leaves the surrounding ``"`` after comment strip, so relative
    absolutization joins the quotes into the path (Bugbot 5093013087).
    """
    value = raw.strip()
    if not value:
        return value
    if value[0] == '"':
        out: list[str] = []
        i = 1
        while i < len(value):
            ch = value[i]
            if ch == "\\":
                if i + 1 >= len(value):
                    out.append("\\")
                    break
                nxt = value[i + 1]
                if nxt == "n":
                    out.append("\n")
                elif nxt == "t":
                    out.append("\t")
                else:
                    # Git: \\ \" and unknown escapes keep the escaped character.
                    out.append(nxt)
                i += 2
                continue
            if ch == '"':
                # Closing quote; remainder is whitespace / comment.
                return "".join(out)
            out.append(ch)
            i += 1
        return "".join(out)
    # Unquoted trailing comments (Git: space/tab then # or ;).
    for idx, ch in enumerate(value):
        if ch in "#;" and idx > 0 and value[idx - 1] in " \t":
            return value[:idx].rstrip()
    return value


def _format_git_config_value(value: str) -> str:
    if any(ch in value for ch in " \t#\"'\\;"):
        escaped = (
            value.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\t", "\\t")
        )
        return f'"{escaped}"'
    return value


def _rewrite_relative_core_worktree_for_snapshot(
    text: str,
    original_git_dir: Path,
) -> str | None:
    """Absolutize relative ``core.worktree`` against the original git-dir.

    Git resolves relative ``core.worktree`` against ``$GIT_DIR``. A verbatim copy
    into a temporary ``--git-dir`` re-bases that path and breaks discovery, so a
    clean nested redirect is treated as a mutation (review 5092778260).
    """
    bom = ""
    body = text
    if body.startswith("\ufeff"):
        bom = "\ufeff"
        body = body[1:]

    in_core = False
    out_lines: list[str] = []
    for line in body.splitlines(keepends=True):
        newline = ""
        content = line
        if content.endswith("\r\n"):
            newline = "\r\n"
            content = content[:-2]
        elif content.endswith("\n"):
            newline = "\n"
            content = content[:-1]
        elif content.endswith("\r"):
            newline = "\r"
            content = content[:-1]

        stripped = content.split(";", 1)[0].split("#", 1)[0].strip()
        section_match = _GIT_ANY_SECTION_HEADER.match(stripped)
        if section_match is not None:
            in_core = bool(_GIT_CORE_SECTION.match(stripped))
            remainder = section_match.group(2).strip()
            if in_core and remainder:
                # Same-line ``[core] worktree = …`` (PRRT_kwDOSJAM6s6etk6T).
                bracket_end = content.find("]")
                if bracket_end >= 0:
                    header_part = content[: bracket_end + 1]
                    assignment_part = content[bracket_end + 1 :]
                    match = _GIT_CORE_WORKTREE_LINE.match(assignment_part)
                    if match is not None:
                        prefix, raw_value, suffix = match.groups()
                        value = _unquote_git_config_value(raw_value)
                        if value and not value.startswith("~") and not Path(value).is_absolute():
                            try:
                                absolute = (original_git_dir / value).resolve()
                            except OSError:
                                return None
                            assignment_part = (
                                f"{prefix}{_format_git_config_value(str(absolute))}{suffix}"
                            )
                            out_lines.append(header_part + assignment_part + newline)
                            continue
            out_lines.append(line)
            continue

        if in_core:
            match = _GIT_CORE_WORKTREE_LINE.match(content)
            if match is not None:
                prefix, raw_value, suffix = match.groups()
                value = _unquote_git_config_value(raw_value)
                if value and not value.startswith("~") and not Path(value).is_absolute():
                    try:
                        absolute = (original_git_dir / value).resolve()
                    except OSError:
                        return None
                    content = f"{prefix}{_format_git_config_value(str(absolute))}{suffix}"
                    out_lines.append(content + newline)
                    continue

        out_lines.append(line)
    return bom + "".join(out_lines)


@contextlib.contextmanager
def untrusted_nested_probe_config_snapshot_git_dir(
    nested_root: Path,
    *,
    containment_roots: Sequence[Path] | None = None,
) -> Iterator[Path | None]:
    """Yield a private git-dir whose local config is a validated snapshot.

    Subsequent nested probes must use this ``--git-dir`` so a surviving agent
    cannot inject ``include.path`` into the live repository config mid-probe
    (PRRT_kwDOSJAM6s6elv_p). Yields ``None`` when materialization fails closed.

    Object/refs/index leaves are private copies read through held fds so a
    post-materialization rename of the live git-dir cannot redirect those paths
    through an attacker symlink at the old pathname (PRRT_kwDOSJAM6s6eXrkk /
    PRRT_kwDOSJAM6s6eX7EK), leaf bytes stay pinned against a post-validation name
    swap (PRRT_kwDOSJAM6s6ercEO), and the control plane does not retain one
    descriptor per nested object/ref until probes finish (PRRT_kwDOSJAM6s6eteRs).
    Config, HEAD, objects, and refs are snapshotted through retained directory
    descriptors rather than resolved git-dir pathnames so a nested-root symlink
    swap after discovery cannot redirect the snapshot (PRRT_kwDOSJAM6s6evMAl).
    """
    git_dirs = _nested_repository_git_dirs_for_include_scan(
        nested_root,
        containment_roots=containment_roots,
    )
    if git_dirs is None or not git_dirs:
        yield None
        return
    nested_fd = _open_nested_root_directory_fd(nested_root)
    if nested_fd is None:
        yield None
        return
    primary_fd: int | None = None
    object_fd: int | None = None
    objects_store_fds: list[int] = []
    refs_store_fds: list[int] = []
    metadata_leaf_fds: list[int] = []
    staging: Path | None = None
    try:
        roots = _nested_git_metadata_containment_roots(
            _pinned_directory_path(nested_fd),
            containment_roots,
        )
        if roots is None:
            yield None
            return
        opened = _open_nested_probe_git_dir_fds(nested_fd, containment_roots=roots)
        if opened is None:
            yield None
            return
        primary_fd, object_fd = opened
        snap_primary = _snapshot_git_dir_local_configs_via_fd(primary_fd)
        if snap_primary is None:
            yield None
            return
        if object_fd != primary_fd:
            snap_object = _snapshot_git_dir_local_configs_via_fd(object_fd)
            if snap_object is None:
                yield None
                return
        else:
            snap_object = snap_primary
        # HEAD is agent-controlled: use the same bounded O_NOFOLLOW|O_NONBLOCK
        # snapshot as config so a symlink/FIFO/growing file cannot leak foreign
        # contents or hang the monitor (PRRT_kwDOSJAM6s6emN9X).
        head_text = _read_git_dir_child_text_via_fd(primary_fd, "HEAD")
        if head_text is None:
            yield None
            return
        if object_fd != primary_fd and "config" in snap_object:
            main_config = snap_object["config"]
        else:
            main_config = snap_primary.get(
                "config",
                "[core]\n\trepositoryformatversion = 0\n",
            )
        worktree_config = snap_primary.get("config.worktree")
        pinned_primary = _pinned_directory_path(primary_fd)
        rewritten_main = _rewrite_relative_core_worktree_for_snapshot(main_config, pinned_primary)
        if rewritten_main is None:
            yield None
            return
        main_config = rewritten_main
        if worktree_config is not None:
            rewritten_wt = _rewrite_relative_core_worktree_for_snapshot(
                worktree_config, pinned_primary
            )
            if rewritten_wt is None:
                yield None
                return
            worktree_config = rewritten_wt

        # Reject existing ``objects/info/alternates`` before probes so foreign
        # stores cannot toggle fingerprint readability (PRRT_kwDOSJAM6s6ep1TL).
        # The snapshot also omits ``objects/info`` so a late-created alternates
        # file after this check cannot reach snapshot-scoped probes
        # (Bugbot 5094509768).
        if _git_dir_declares_object_alternates(object_fd):
            yield None
            return

        staging = Path(tempfile.mkdtemp(prefix="awf-nested-git-probe-"))
        # Config text is decoded with surrogateescape; rewrite the same way as
        # HEAD so non-UTF-8 comment/value bytes survive the probe snapshot
        # (PRRT_kwDOSJAM6s6emdqr).
        (staging / "config").write_bytes(main_config.encode("utf-8", errors="surrogateescape"))
        if worktree_config is not None:
            (staging / "config.worktree").write_bytes(
                worktree_config.encode("utf-8", errors="surrogateescape")
            )
        # Refuse symlinked nested ref/object/index stores: staging links would
        # chain into foreign workspaces and poison residue attribution
        # (PRRT_kwDOSJAM6s6eqQgm). Materialize ``objects`` without ``info`` so
        # ``alternates`` cannot leak through the snapshot (Bugbot 5094509768),
        # and without linking whole fan-out directories so nested loose-object
        # symlinks cannot either (PRRT_kwDOSJAM6s6eq1r3). Materialize ``refs``
        # the same way so nested loose-ref symlinks cannot spoof HEAD
        # (PRRT_kwDOSJAM6s6ercEL). Object/ref leaves are private copies so the
        # materializers release their walk fds before yield
        # (PRRT_kwDOSJAM6s6eteRs); only index/packed-refs/sharedindex directory
        # pins (if any) and git-dir fds remain across the probe.
        objects_ok, objects_store_fds = _symlink_nested_probe_objects_store_via_fd(
            object_fd, staging
        )
        if not objects_ok:
            yield None
            return
        refs_ok, refs_store_fds = _symlink_nested_probe_refs_store_via_fd(object_fd, staging)
        if not refs_ok:
            yield None
            return
        if not _symlink_git_dir_child_via_fd(
            object_fd,
            "packed-refs",
            staging / "packed-refs",
            metadata_leaf_fds,
            expect_directory=False,
        ):
            yield None
            return
        # Git rejects a git-dir whose HEAD is a symlink ("not a git repository").
        (staging / "HEAD").write_bytes(head_text.encode("utf-8", errors="surrogateescape"))
        if not _symlink_git_dir_child_via_fd(
            primary_fd,
            "index",
            staging / "index",
            metadata_leaf_fds,
            expect_directory=False,
        ):
            yield None
            return
        # Split-index stores the bulk of the index in ``sharedindex.<oid>``;
        # omit those and ``diff-files`` fails closed as unreadable (PRRT_kwDOSJAM6s6eo3py).
        if not _symlink_split_index_backing_files_via_fd(primary_fd, staging, metadata_leaf_fds):
            yield None
            return
        # Do not symlink live ``info``: ``ls-files -o --exclude-standard`` would
        # still honor repository-local ``info/exclude`` through that link while
        # HEAD and tracked digests stay unchanged (PRRT_kwDOSJAM6s6enFGg).
        yield staging
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        for metadata_leaf_fd in metadata_leaf_fds:
            with contextlib.suppress(OSError):
                os.close(metadata_leaf_fd)
        for objects_store_fd in objects_store_fds:
            with contextlib.suppress(OSError):
                os.close(objects_store_fd)
        for refs_store_fd in refs_store_fds:
            with contextlib.suppress(OSError):
                os.close(refs_store_fd)
        if object_fd is not None and object_fd != primary_fd:
            os.close(object_fd)
        if primary_fd is not None:
            os.close(primary_fd)
        os.close(nested_fd)


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
