"""Filesystem ownership and Git environment helpers for worktree management."""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import stat
import struct
import time
import zlib
from collections.abc import Iterator, Mapping, Sequence
from contextvars import ContextVar, Token
from pathlib import Path

from awf.node import git_manager_ownership_store as _ownership_store

_format_git_config_value = _ownership_store._format_git_config_value
_git_dir_declares_object_alternates = _ownership_store._git_dir_declares_object_alternates
_open_contained_directory_nofollow = _ownership_store._open_contained_directory_nofollow
_open_git_dir_child_directory_fd = _ownership_store._open_git_dir_child_directory_fd
_open_git_dir_directory_fd = _ownership_store._open_git_dir_directory_fd
_open_git_metadata_candidate = _ownership_store._open_git_metadata_candidate
_open_nested_probe_git_dir_fds = _ownership_store._open_nested_probe_git_dir_fds
_open_nested_root_directory_fd = _ownership_store._open_nested_root_directory_fd
_open_relative_directory_from_dir_fd = _ownership_store._open_relative_directory_from_dir_fd
_pinned_directory_path = _ownership_store._pinned_directory_path
_proc_self_fd_number = _ownership_store._proc_self_fd_number
_rewrite_relative_core_worktree_for_snapshot = (
    _ownership_store._rewrite_relative_core_worktree_for_snapshot
)
_symlink_nested_probe_objects_store_via_fd = (
    _ownership_store._symlink_nested_probe_objects_store_via_fd
)
_symlink_nested_probe_refs_store_via_fd = _ownership_store._symlink_nested_probe_refs_store_via_fd
_symlink_object_store_tree_via_fd = _ownership_store._symlink_object_store_tree_via_fd
_unquote_git_config_value = _ownership_store._unquote_git_config_value
untrusted_nested_probe_config_snapshot_git_dir = (
    _ownership_store.untrusted_nested_probe_config_snapshot_git_dir
)

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
# Aggregate cap across every ``config`` / ``config.worktree`` snapshot in one
# worktree fingerprint so thousands of nested git-dirs cannot retain tens of GiB
# after directory enumeration ends (PRRT_kwDOSJAM6s6e7pGD).
_GIT_CONFIG_SNAPSHOT_AGGREGATE_MAX_BYTES = 32 * 1024 * 1024
_GIT_CONFIG_SNAPSHOT_BUDGET_SECONDS = 30.0

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
# Shared cap across all copied leaves in one walk: per-leaf max alone still
# allows many sub-64-MiB packs to fill /tmp before the enum deadline
# (PRRT_kwDOSJAM6s6e30Ru). 4× leaf max matches the worktree hash aggregate ratio.
_OBJECT_STORE_ENUM_AGGREGATE_MAX_BYTES = 4 * _OBJECT_STORE_LEAF_COPY_MAX_BYTES
_OBJECT_STORE_LEAF_COPY_BUDGET_SECONDS = 30.0
_OBJECT_STORE_LEAF_COPY_CHUNK_BYTES = 64 * 1024
# Git loose objects are zlib(type + SP + decimal-size + NUL + payload). Nested
# probes must reject declared payloads above the leaf max even when compressed
# on-disk bytes fit (PRRT_kwDOSJAM6s6evsX8). Bound header inflate only, and also
# cap compressed bytes + wall time so empty DEFLATE blocks cannot burn the
# nested-probe budget before the copy deadline starts (PRRT_kwDOSJAM6s6ewJZe).
# Peek-budget exhaustion is distinct from "not a loose object" so staging fails
# closed instead of copying a padded zip-bomb (PRRT_kwDOSJAM6s6ewp-Z).
_GIT_LOOSE_OBJECT_TYPES = frozenset({b"blob", b"tree", b"commit", b"tag"})
_GIT_LOOSE_OBJECT_HEADER_MAX_BYTES = 64
_GIT_LOOSE_OBJECT_PEEK_COMPRESSED_CHUNK = 4096
_GIT_LOOSE_OBJECT_PEEK_COMPRESSED_MAX_BYTES = 16 * 1024
_GIT_LOOSE_OBJECT_PEEK_BUDGET_SECONDS = 2.0


class _GitLooseObjectPeekBudgetExhausted:
    """Sentinel: peek hit compressed-byte or wall-time budget before a header NUL."""

    __slots__ = ()


_GIT_LOOSE_OBJECT_PEEK_BUDGET_EXHAUSTED = _GitLooseObjectPeekBudgetExhausted()


class _GitConfigSnapshotBudget:
    """Mutable aggregate byte + deadline budget for worktree Git config snapshots."""

    __slots__ = ("bytes_remaining", "deadline")

    def __init__(self, *, bytes_remaining: int, deadline: float) -> None:
        self.bytes_remaining = bytes_remaining
        self.deadline = deadline


_GIT_CONFIG_SNAPSHOT_BUDGET: ContextVar[_GitConfigSnapshotBudget | None] = ContextVar(
    "_git_config_snapshot_budget",
    default=None,
)


class _ObjectStoreEnumBudget:
    """Mutable aggregate entry + byte + depth + deadline budget for object-store walks."""

    __slots__ = ("entries_remaining", "bytes_remaining", "deadline", "max_depth")

    def __init__(
        self,
        *,
        entries_remaining: int,
        bytes_remaining: int,
        deadline: float,
        max_depth: int,
    ) -> None:
        self.entries_remaining = entries_remaining
        self.bytes_remaining = bytes_remaining
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

# Force case-sensitive path comparison when an agent sets ``core.ignoreCase=true``
# on a case-sensitive worker: otherwise ``status`` / ``ls-files -o`` / ``clean``
# treat ``FOO`` beside tracked ``foo`` as the same path and omit the untracked
# residue (PRRT_kwDOSJAM6s6exXso nested; PRRT_kwDOSJAM6s6ex8lZ ordinary).
FORCE_CASE_SENSITIVE_PATHS_GIT_CONFIG_ARGS: tuple[str, ...] = (
    "-c",
    "core.ignoreCase=false",
)

# Force ``core.fileMode=true``: with local ``core.fileMode=false``, status /
# ``diff`` / ``diff-files`` omit executable-bit flips and ``restore`` /
# ``reset --hard`` leave them behind (nested PRRT_kwDOSJAM6s6ekF15;
# ordinary PRRT_kwDOSJAM6s6ey_47).
FORCE_FILE_MODE_TRACKING_GIT_CONFIG_ARGS: tuple[str, ...] = (
    "-c",
    "core.fileMode=true",
)

# Force ``core.symlinks=true``: with local ``core.symlinks=false``, status /
# ``diff`` / ``diff-files`` omit symlink→file typechanges when link text matches,
# and ``restore`` / ``reset --hard`` leave the regular file behind (nested review
# 5093517929; ordinary PRRT_kwDOSJAM6s6ezrHU).
FORCE_SYMLINK_TRACKING_GIT_CONFIG_ARGS: tuple[str, ...] = (
    "-c",
    "core.symlinks=true",
)

# Clear ``core.fsmonitor``: an agent-set fsmonitor hook can prime the index then
# omit later tracked edits from ``git status``, so ordinary residue fingerprints
# and cleanup cleanliness checks report clean while dirty bytes remain
# (ordinary PRRT_kwDOSJAM6s6e0BJS; nested PRRT_kwDOSJAM6s6eV4s0).
DISABLE_LOCAL_FSMONITOR_GIT_CONFIG_ARGS: tuple[str, ...] = (
    "-c",
    "core.fsmonitor=",
)

# Force full stat comparison: with ``core.trustctime=false`` or
# ``core.checkStat=minimal``, ``git status`` can miss a same-size overwrite that
# restores the indexed mtime (no index hide flags required), so ordinary residue
# fingerprints and cleanup cleanliness collide while dirty bytes remain
# (PRRT_kwDOSJAM6s6e1yPZ).
FORCE_FULL_STAT_CHECK_GIT_CONFIG_ARGS: tuple[str, ...] = (
    "-c",
    "core.trustctime=true",
    "-c",
    "core.checkStat=default",
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
# Force ``core.fileMode=true`` via ``FORCE_FILE_MODE_TRACKING_GIT_CONFIG_ARGS``.
# Force ``core.symlinks=true`` via ``FORCE_SYMLINK_TRACKING_GIT_CONFIG_ARGS``.
# Force ``core.ignoreCase=false`` so case-collision untracked residue stays visible
# (PRRT_kwDOSJAM6s6exXso).
# Clear ``core.fsmonitor`` via ``DISABLE_LOCAL_FSMONITOR_GIT_CONFIG_ARGS``.
# Force full stat checks via ``FORCE_FULL_STAT_CHECK_GIT_CONFIG_ARGS``
# (PRRT_kwDOSJAM6s6e1yPZ).
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
    *FORCE_CASE_SENSITIVE_PATHS_GIT_CONFIG_ARGS,
    "-c",
    f"core.excludesFile={os.devnull}",
    *DISABLE_LOCAL_FSMONITOR_GIT_CONFIG_ARGS,
    *FORCE_FULL_STAT_CHECK_GIT_CONFIG_ARGS,
    *FORCE_FILE_MODE_TRACKING_GIT_CONFIG_ARGS,
    *FORCE_SYMLINK_TRACKING_GIT_CONFIG_ARGS,
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


def _git_config_snapshot_budget_allows_and_consume(size: int) -> bool:
    """Reserve ``size`` bytes from the active config-snapshot budget, or allow when unset."""
    budget = _GIT_CONFIG_SNAPSHOT_BUDGET.get()
    if budget is None:
        return True
    if time.monotonic() >= budget.deadline:
        return False
    if size < 0 or size > budget.bytes_remaining:
        return False
    budget.bytes_remaining -= size
    return True


def _git_config_snapshot_budget_past_deadline() -> bool:
    budget = _GIT_CONFIG_SNAPSHOT_BUDGET.get()
    return budget is not None and time.monotonic() >= budget.deadline


@contextlib.contextmanager
def _residue_git_config_snapshot_budget() -> Iterator[None]:
    """Bound aggregate Git config snapshot bytes and wall time for one fingerprint."""
    if _GIT_CONFIG_SNAPSHOT_BUDGET.get() is not None:
        yield
        return
    budget = _GitConfigSnapshotBudget(
        bytes_remaining=_GIT_CONFIG_SNAPSHOT_AGGREGATE_MAX_BYTES,
        deadline=time.monotonic() + _GIT_CONFIG_SNAPSHOT_BUDGET_SECONDS,
    )
    token: Token[_GitConfigSnapshotBudget | None] = _GIT_CONFIG_SNAPSHOT_BUDGET.set(budget)
    try:
        yield
    finally:
        _GIT_CONFIG_SNAPSHOT_BUDGET.reset(token)


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
        if not _git_config_snapshot_budget_allows_and_consume(st.st_size):
            return None
        deadline = time.monotonic() + _GIT_DIR_CONFIG_READ_BUDGET_SECONDS
        remaining = st.st_size
        chunks: list[bytes] = []
        while remaining > 0:
            if time.monotonic() >= deadline:
                return None
            if _git_config_snapshot_budget_past_deadline():
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
        except FileNotFoundError:
            continue
        except OSError:
            # EACCES / other non-ENOENT: config may exist but be unreadable;
            # fail closed rather than treating as absent (PRRT_kwDOSJAM6s6evrZl).
            return True
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
            # EACCES / other non-ENOENT: config may exist but be unreadable;
            # fail closed rather than treating as absent (PRRT_kwDOSJAM6s6evrZl).
            return None
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
        except FileNotFoundError:
            continue
        except OSError:
            # EACCES / other non-ENOENT: config may exist but be unreadable;
            # fail closed rather than treating as absent (PRRT_kwDOSJAM6s6evrZl).
            return None
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


def _parse_git_loose_object_header_declared_size(header: bytes) -> int | None:
    """Return declared payload size from a loose-object header, or ``None``."""
    try:
        obj_type, size_text = header.split(b" ", 1)
    except ValueError:
        return None
    if obj_type not in _GIT_LOOSE_OBJECT_TYPES:
        return None
    if not size_text.isdigit():
        return None
    try:
        return int(size_text)
    except ValueError:  # pragma: no cover - isdigit() already guards
        return None


def _git_loose_object_declared_size_from_fd(
    fd: int,
    *,
    max_compressed_bytes: int = _GIT_LOOSE_OBJECT_PEEK_COMPRESSED_MAX_BYTES,
    budget_seconds: float = _GIT_LOOSE_OBJECT_PEEK_BUDGET_SECONDS,
) -> int | None | _GitLooseObjectPeekBudgetExhausted:
    """Peek declared payload size from a Git loose object without full inflate.

    Reads from the current offset and leaves the cursor advanced; callers must
    ``lseek`` back before copying. Returns ``None`` when the stream is not a
    parseable loose object (packs, indexes, junk). Returns
    ``_GIT_LOOSE_OBJECT_PEEK_BUDGET_EXHAUSTED`` when the compressed-byte /
    wall-time peek budget is exhausted before a header NUL
    (PRRT_kwDOSJAM6s6ewJZe, PRRT_kwDOSJAM6s6ewp-Z) so callers fail closed
    instead of treating budget exhaustion as a non-object.
    """
    decompressor = zlib.decompressobj()
    inflated = bytearray()
    compressed_read = 0
    deadline = time.monotonic() + budget_seconds
    while len(inflated) <= _GIT_LOOSE_OBJECT_HEADER_MAX_BYTES:
        if time.monotonic() >= deadline:
            return _GIT_LOOSE_OBJECT_PEEK_BUDGET_EXHAUSTED
        if compressed_read >= max_compressed_bytes:
            return _GIT_LOOSE_OBJECT_PEEK_BUDGET_EXHAUSTED
        try:
            to_read = min(
                _GIT_LOOSE_OBJECT_PEEK_COMPRESSED_CHUNK,
                max_compressed_bytes - compressed_read,
            )
            chunk = os.read(fd, to_read)
        except OSError:
            return None
        if not chunk:
            return None
        compressed_read += len(chunk)
        feed = decompressor.unconsumed_tail + chunk if decompressor.unconsumed_tail else chunk
        try:
            room = _GIT_LOOSE_OBJECT_HEADER_MAX_BYTES + 1 - len(inflated)
            piece = decompressor.decompress(feed, max_length=room)
        except zlib.error:
            return None
        inflated.extend(piece)
        nul = inflated.find(b"\0")
        if nul >= 0:
            return _parse_git_loose_object_header_declared_size(bytes(inflated[:nul]))
        if len(inflated) > _GIT_LOOSE_OBJECT_HEADER_MAX_BYTES:
            return None
    return None


def _copy_opened_regular_file_to_path(
    fd: int,
    dest: Path,
    *,
    max_bytes: int = _OBJECT_STORE_LEAF_COPY_MAX_BYTES,
    budget_seconds: float = _OBJECT_STORE_LEAF_COPY_BUDGET_SECONDS,
    validate_git_loose_object: bool = False,
    enum_budget: _ObjectStoreEnumBudget | None = None,
) -> bool:
    """Stream a size/deadline-bounded private copy from an opened regular file.

    Used for nested-probe staging leaves so callers can close ``fd`` immediately
    instead of retaining one descriptor per object/ref until probes finish
    (PRRT_kwDOSJAM6s6eteRs). When ``validate_git_loose_object`` is set, also
    reject parseable loose objects whose declared uncompressed payload exceeds
    ``max_bytes`` (PRRT_kwDOSJAM6s6evsX8), and reject when the header peek
    exhausts its compressed-byte / wall-time budget before parsing a size
    (PRRT_kwDOSJAM6s6ewp-Z). When ``enum_budget`` is set, charge the opened
    descriptor's ``fstat`` size against the shared aggregate so a post-pathname
    grow cannot bypass the walk cap (PRRT_kwDOSJAM6s6fDL6r). Returns ``False``
    on type/size/stability failures.
    """
    try:
        st = os.fstat(fd)
    except OSError:
        return False
    if not stat.S_ISREG(st.st_mode):
        return False
    if st.st_size < 0 or st.st_size > max_bytes:
        return False
    if enum_budget is not None:
        if st.st_size > enum_budget.bytes_remaining:
            return False
        enum_budget.bytes_remaining -= st.st_size
    if validate_git_loose_object:
        declared = _git_loose_object_declared_size_from_fd(fd)
        try:
            os.lseek(fd, 0, os.SEEK_SET)
        except OSError:
            return False
        if declared is _GIT_LOOSE_OBJECT_PEEK_BUDGET_EXHAUSTED:
            return False
        if isinstance(declared, int) and declared > max_bytes:
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
    validate_git_loose_object: bool = False,
    enum_budget: _ObjectStoreEnumBudget | None = None,
) -> bool:
    """Materialize ``dest`` from the opened inode of ``name`` under ``dir_fd``.

    Absolute pathnames into ``.git/...`` break under a post-open rename of the
    git-dir (attacker plants a symlink at the old path). Directory pins still
    use ``/proc/<pid>/fd/<child_fd>``. Regular-file leaves are copied through the
    validated child fd into a private staging file so inode bytes stay pinned
    against a post-validation name swap (PRRT_kwDOSJAM6s6ercEO) without retaining
    one descriptor per object/ref leaf for the probe lifetime
    (PRRT_kwDOSJAM6s6eteRs). Pass ``enum_budget`` so leaf copies charge the
    opened inode size against the shared object-store walk cap
    (PRRT_kwDOSJAM6s6fDL6r).

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
        if not _copy_opened_regular_file_to_path(
            child_fd,
            dest,
            validate_git_loose_object=validate_git_loose_object,
            enum_budget=enum_budget,
        ):
            return False
    finally:
        os.close(child_fd)
    return True


def _read_fd_regular_file_bytes(
    fd: int,
    *,
    max_bytes: int,
    apply_config_snapshot_budget: bool = False,
) -> bytes | None:
    """Return a size/deadline-bounded snapshot of an already-opened regular file."""
    try:
        st = os.fstat(fd)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    if st.st_size < 0 or st.st_size > max_bytes:
        return None
    if apply_config_snapshot_budget and not _git_config_snapshot_budget_allows_and_consume(
        st.st_size
    ):
        return None
    deadline = time.monotonic() + _GIT_DIR_CONFIG_READ_BUDGET_SECONDS
    remaining = st.st_size
    chunks: list[bytes] = []
    while remaining > 0:
        if time.monotonic() >= deadline:
            return None
        if apply_config_snapshot_budget and _git_config_snapshot_budget_past_deadline():
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


def _read_git_dir_child_bytes_via_fd(
    dir_fd: int,
    name: str,
    *,
    max_bytes: int,
    apply_config_snapshot_budget: bool = False,
) -> bytes | None:
    """Bounded ``openat`` read of a git-dir child; ``None`` fails closed."""
    try:
        fd = os.open(name, _GIT_DIR_CONFIG_OPEN_FLAGS, dir_fd=dir_fd)
    except OSError:
        return None
    try:
        return _read_fd_regular_file_bytes(
            fd,
            max_bytes=max_bytes,
            apply_config_snapshot_budget=apply_config_snapshot_budget,
        )
    finally:
        os.close(fd)


def _read_git_dir_child_text_via_fd(dir_fd: int, name: str) -> str | None:
    """Bounded ``openat`` text snapshot of a git-dir child; ``None`` fails closed."""
    raw = _read_git_dir_child_bytes_via_fd(
        dir_fd,
        name,
        max_bytes=_GIT_DIR_CONFIG_MAX_BYTES,
        apply_config_snapshot_budget=True,
    )
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
