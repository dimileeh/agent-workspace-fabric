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
from collections.abc import Iterator, Mapping
from pathlib import Path

_GIT_INCLUDE_SECTION = re.compile(r"^\[include\]\s*$", re.IGNORECASE)
_GIT_INCLUDE_IF_SECTION = re.compile(r"^\[includeIf\b", re.IGNORECASE)
_GIT_CONFIG_SECTION = re.compile(r"^\[")
_GIT_CONFIG_PATH_KEY = re.compile(r"^path\s*=", re.IGNORECASE)
_GIT_CORE_SECTION = re.compile(r"^\[core\]\s*$", re.IGNORECASE)
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
    # Git accepts a UTF-8 BOM on config files; keep scanning aligned so a BOM
    # attached to ``[include]`` / ``[includeIf`` cannot bypass the guard
    # (PRRT_kwDOSJAM6s6elA2I).
    if text.startswith("\ufeff"):
        text = text[1:]
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


def _nested_repository_git_dirs_for_include_scan(
    nested_root: Path,
) -> tuple[Path, ...] | None:
    """Return git-dirs whose local config Git would load for ``nested_root`` probes.

    Returns ``None`` to fail closed when a regular gitfile/commondir cannot be
    snapshotted safely (PRRT_kwDOSJAM6s6elA2N).
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
            # Present regular file but unsafe/unstable/oversized snapshot.
            return True
        if git_config_text_declares_includes(text):
            return True
    return False


def untrusted_nested_repository_local_config_has_includes(nested_root: Path) -> bool:
    """Return True when an embedded repo's local config declares includes.

    Repository-local ``include.path`` / ``includeIf`` still load during nested
    probes despite ``UNTRUSTED_NESTED_GIT_CONFIG_ARGS``; callers must fail closed
    before invoking Git (PRRT_kwDOSJAM6s6ekfTU).
    """
    git_dirs = _nested_repository_git_dirs_for_include_scan(nested_root)
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


def _symlink_git_dir_child_via_fd(dir_fd: int, name: str, dest: Path) -> None:
    """Link ``dest`` to ``name`` under an open git-dir fd.

    Absolute pathnames into ``.git/...`` break under a post-open rename of the
    git-dir (attacker plants a symlink at the old path). Links through
    ``/proc/<pid>/fd/<fd>/`` resolve the still-open directory inode in this
    process and remain valid for child ``git`` invocations for the probe
    lifetime.
    """
    try:
        os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        return
    dest.symlink_to(Path(f"/proc/{os.getpid()}/fd/{dir_fd}") / name)


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


def _symlink_split_index_backing_files_via_fd(dir_fd: int, staging: Path) -> None:
    """Link the single ``sharedindex.<oid>`` referenced by a split-index ``index``.

    Snapshotting only ``index`` omits the referenced shared-index backing file,
    so snapshot-scoped ``diff-files`` exits 128 with ``index file open failed``
    (PRRT_kwDOSJAM6s6eo3py). Resolve the OID from the ``link`` extension instead
    of enumerating every ``sharedindex.*`` name under the agent-controlled
    git-dir (PRRT_kwDOSJAM6s6epUot). Open the index through the held directory
    fd so a post-open rename cannot redirect the read.
    """
    index_bytes = _read_git_dir_child_bytes_via_fd(
        dir_fd, "index", max_bytes=_GIT_SPLIT_INDEX_MAX_BYTES
    )
    if index_bytes is None:
        return
    oid_hex = _split_index_shared_oid_hex(index_bytes)
    if oid_hex is None:
        return
    name = f"sharedindex.{oid_hex}"
    if (
        _GIT_SHARED_INDEX_NAME.fullmatch(name) is None
    ):  # pragma: no cover - oid.hex() always matches
        return
    _symlink_git_dir_child_via_fd(dir_fd, name, staging / name)


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


def _git_dir_declares_object_alternates(object_fd: int) -> bool:
    """Return True when ``objects/info/alternates`` is present or unreadable.

    Nested probe snapshots symlink the live ``objects`` tree, so a repository
    ``alternates`` file (gitrepository-layout) would still be honored and could
    resolve objects from another workspace store (PRRT_kwDOSJAM6s6ep1TL). Missing
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
        if stripped.startswith("[") and stripped.endswith("]"):
            in_core = bool(_GIT_CORE_SECTION.match(stripped))
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
) -> Iterator[Path | None]:
    """Yield a private git-dir whose local config is a validated snapshot.

    Subsequent nested probes must use this ``--git-dir`` so a surviving agent
    cannot inject ``include.path`` into the live repository config mid-probe
    (PRRT_kwDOSJAM6s6elv_p). Yields ``None`` when materialization fails closed.

    Object/refs/index links go through held directory fds
    (``/proc/<pid>/fd/<n>/``) so a post-materialization rename of the live
    git-dir cannot redirect those links through an attacker symlink at the old
    pathname (PRRT_kwDOSJAM6s6eXrkk / PRRT_kwDOSJAM6s6eX7EK).
    """
    git_dirs = _nested_repository_git_dirs_for_include_scan(nested_root)
    if git_dirs is None or not git_dirs:
        yield None
        return
    snapshots: list[dict[str, str]] = []
    for git_dir in git_dirs:
        snap = _snapshot_git_dir_local_configs(git_dir)
        if snap is None:
            yield None
            return
        snapshots.append(snap)

    primary = git_dirs[0]
    # HEAD is agent-controlled: use the same bounded O_NOFOLLOW|O_NONBLOCK
    # snapshot as config so a symlink/FIFO/growing file cannot leak foreign
    # contents or hang the monitor (PRRT_kwDOSJAM6s6emN9X).
    head_text = _read_git_dir_config_text(primary / "HEAD")
    if head_text is None:
        yield None
        return
    common = git_dirs[1] if len(git_dirs) > 1 else None
    object_root = common if common is not None else primary
    if common is not None and "config" in snapshots[1]:
        main_config = snapshots[1]["config"]
    else:
        main_config = snapshots[0].get(
            "config",
            "[core]\n\trepositoryformatversion = 0\n",
        )
    worktree_config = snapshots[0].get("config.worktree")

    rewritten_main = _rewrite_relative_core_worktree_for_snapshot(main_config, primary)
    if rewritten_main is None:
        yield None
        return
    main_config = rewritten_main
    if worktree_config is not None:
        rewritten_wt = _rewrite_relative_core_worktree_for_snapshot(worktree_config, primary)
        if rewritten_wt is None:
            yield None
            return
        worktree_config = rewritten_wt

    primary_fd = _open_git_dir_directory_fd(primary)
    if primary_fd is None:
        yield None
        return
    object_fd: int | None = None
    staging: Path | None = None
    try:
        if object_root != primary:
            object_fd = _open_git_dir_directory_fd(object_root)
            if object_fd is None:
                yield None
                return
        else:
            object_fd = primary_fd

        # Symlinking live ``objects`` preserves ``objects/info/alternates``;
        # reject that metadata before probes so foreign stores cannot toggle
        # fingerprint readability (PRRT_kwDOSJAM6s6ep1TL).
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
        for name in ("objects", "refs"):
            _symlink_git_dir_child_via_fd(object_fd, name, staging / name)
        _symlink_git_dir_child_via_fd(object_fd, "packed-refs", staging / "packed-refs")
        # Git rejects a git-dir whose HEAD is a symlink ("not a git repository").
        (staging / "HEAD").write_bytes(head_text.encode("utf-8", errors="surrogateescape"))
        _symlink_git_dir_child_via_fd(primary_fd, "index", staging / "index")
        # Split-index stores the bulk of the index in ``sharedindex.<oid>``;
        # omit those and ``diff-files`` fails closed as unreadable (PRRT_kwDOSJAM6s6eo3py).
        _symlink_split_index_backing_files_via_fd(primary_fd, staging)
        # Do not symlink live ``info``: ``ls-files -o --exclude-standard`` would
        # still honor repository-local ``info/exclude`` through that link while
        # HEAD and tracked digests stay unchanged (PRRT_kwDOSJAM6s6enFGg).
        yield staging
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        if object_fd is not None and object_fd != primary_fd:
            os.close(object_fd)
        os.close(primary_fd)


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
