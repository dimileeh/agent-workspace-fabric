"""Git-dir FD openers, object/ref store staging, and nested probe config snapshots.

Kept separate so ``git_manager_ownership`` stays under the first-party line budget.
Helpers remain available on ``git_manager_ownership`` via re-exports.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import stat
import tempfile
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from awf.node.git_manager_ownership import _ObjectStoreEnumBudget


def _own() -> ModuleType:
    """Lazy facade import so ownership can re-export this module at import time."""
    from awf.node import git_manager_ownership as ownership

    return ownership


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
    fd_no = _own()._proc_self_fd_number(nested_root)
    if fd_no is None:
        return cast(int | None, _own()._open_git_dir_directory_fd(nested_root))
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
            next_fd = _own()._open_git_dir_child_directory_fd(current, part)
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
        root_fd = _own()._open_git_dir_directory_fd(resolved_root)
        if root_fd is None:
            continue
        walked: int | None = None
        try:
            walked = _own()._open_relative_directory_from_dir_fd(root_fd, relative)
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
    probe = (
        candidate if candidate.is_absolute() else _own()._pinned_directory_path(base_fd) / candidate
    )
    if _own()._resolved_git_metadata_within_roots(probe, containment_roots) is None:
        return None
    if not candidate.is_absolute():
        return cast(int | None, _own()._open_relative_directory_from_dir_fd(base_fd, candidate))
    try:
        relative = probe.resolve().relative_to(_own()._pinned_directory_path(base_fd).resolve())
    except (OSError, ValueError):
        relative = None
    if relative is not None:
        return cast(int | None, _own()._open_relative_directory_from_dir_fd(base_fd, relative))
    return cast(int | None, _own()._open_contained_directory_nofollow(probe, containment_roots))


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
        primary_fd = _own()._open_git_dir_child_directory_fd(nested_fd, ".git")
        if primary_fd is None:
            return None
        if (
            _own()._resolved_git_metadata_within_roots(
                _own()._pinned_directory_path(primary_fd),
                containment_roots,
            )
            is None
        ):
            os.close(primary_fd)
            return None
    elif stat.S_ISREG(marker_mode):
        text = _own()._read_git_dir_child_text_via_fd(nested_fd, ".git")
        if text is None:
            return None
        prefix = "gitdir:"
        if not text.startswith(prefix):
            return None
        git_dir = Path(text[len(prefix) :].strip())
        if not git_dir.parts:
            return None
        primary_fd = _own()._open_git_metadata_candidate(
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
    common_text = _own()._read_git_dir_child_text_via_fd(primary_fd, "commondir")
    if common_text is None:
        os.close(primary_fd)
        return None
    common = Path(common_text.strip())
    if not common.parts:
        return primary_fd, primary_fd
    common_fd = _own()._open_git_metadata_candidate(
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
    objects_fd = _own()._open_git_dir_child_directory_fd(object_fd, "objects")
    if objects_fd is None:
        return True
    try:
        try:
            os.stat("info", dir_fd=objects_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        except OSError:
            return True
        info_fd = _own()._open_git_dir_child_directory_fd(objects_fd, "info")
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
    entry + byte + depth + wall-time budget so a path flood cannot
    ``listdir``-buffer unbounded names, recurse past the worktree depth scale,
    create staging links past the nested-probe scan window, or privately copy
    unbounded leaf bytes into ``/tmp`` (PRRT_kwDOSJAM6s6eq1r7 /
    Bugbot 5094985052 / PRRT_kwDOSJAM6s6e30Ru).
    """
    if budget is None:
        budget = _own()._ObjectStoreEnumBudget(
            entries_remaining=_own()._OBJECT_STORE_ENUM_AGGREGATE_MAX_ENTRIES,
            bytes_remaining=_own()._OBJECT_STORE_ENUM_AGGREGATE_MAX_BYTES,
            deadline=time.monotonic() + _own()._OBJECT_STORE_ENUM_BUDGET_SECONDS,
            max_depth=_own()._OBJECT_STORE_ENUM_MAX_DEPTH,
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
                    # Pathname size is only a fail-fast hint: charge the opened
                    # inode in the copier so a post-stat grow cannot under-report
                    # against the shared aggregate (PRRT_kwDOSJAM6s6fDL6r).
                    if st.st_size < 0 or st.st_size > budget.bytes_remaining:
                        return False
                    if not _own()._symlink_git_dir_child_via_fd(
                        dir_fd,
                        name,
                        staging_dir / name,
                        held_fds,
                        expect_directory=False,
                        validate_git_loose_object=True,
                        enum_budget=budget,
                    ):
                        return False
                    continue
                if not stat.S_ISDIR(st.st_mode):
                    return False
                child_fd = _own()._open_git_dir_child_directory_fd(dir_fd, name)
                if child_fd is None:
                    return False
                child_staging = staging_dir / name
                try:
                    child_staging.mkdir()
                except OSError:
                    os.close(child_fd)
                    return False
                try:
                    if not _own()._symlink_object_store_tree_via_fd(
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
    at check time and for late creation after ``_own()._git_dir_declares_object_alternates``
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
    objects_fd = _own()._open_git_dir_child_directory_fd(object_fd, "objects")
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
        budget = _own()._ObjectStoreEnumBudget(
            entries_remaining=_own()._OBJECT_STORE_ENUM_AGGREGATE_MAX_ENTRIES,
            bytes_remaining=_own()._OBJECT_STORE_ENUM_AGGREGATE_MAX_BYTES,
            deadline=time.monotonic() + _own()._OBJECT_STORE_ENUM_BUDGET_SECONDS,
            max_depth=_own()._OBJECT_STORE_ENUM_MAX_DEPTH,
        )
        if not _own()._symlink_object_store_tree_via_fd(
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
    refs_fd = _own()._open_git_dir_child_directory_fd(object_fd, "refs")
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
        budget = _own()._ObjectStoreEnumBudget(
            entries_remaining=_own()._OBJECT_STORE_ENUM_AGGREGATE_MAX_ENTRIES,
            bytes_remaining=_own()._OBJECT_STORE_ENUM_AGGREGATE_MAX_BYTES,
            deadline=time.monotonic() + _own()._OBJECT_STORE_ENUM_BUDGET_SECONDS,
            max_depth=_own()._OBJECT_STORE_ENUM_MAX_DEPTH,
        )
        if not _own()._symlink_object_store_tree_via_fd(
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
        section_match = _own()._GIT_ANY_SECTION_HEADER.match(stripped)
        if section_match is not None:
            in_core = bool(_own()._GIT_CORE_SECTION.match(stripped))
            remainder = section_match.group(2).strip()
            if in_core and remainder:
                # Same-line ``[core] worktree = …`` (PRRT_kwDOSJAM6s6etk6T).
                bracket_end = content.find("]")
                if bracket_end >= 0:
                    header_part = content[: bracket_end + 1]
                    assignment_part = content[bracket_end + 1 :]
                    match = _own()._GIT_CORE_WORKTREE_LINE.match(assignment_part)
                    if match is not None:
                        prefix, raw_value, suffix = match.groups()
                        value = _own()._unquote_git_config_value(raw_value)
                        if value and not value.startswith("~") and not Path(value).is_absolute():
                            try:
                                absolute = (original_git_dir / value).resolve()
                            except OSError:
                                return None
                            assignment_part = (
                                f"{prefix}{_own()._format_git_config_value(str(absolute))}{suffix}"
                            )
                            out_lines.append(header_part + assignment_part + newline)
                            continue
            out_lines.append(line)
            continue

        if in_core:
            match = _own()._GIT_CORE_WORKTREE_LINE.match(content)
            if match is not None:
                prefix, raw_value, suffix = match.groups()
                value = _own()._unquote_git_config_value(raw_value)
                if value and not value.startswith("~") and not Path(value).is_absolute():
                    try:
                        absolute = (original_git_dir / value).resolve()
                    except OSError:
                        return None
                    content = f"{prefix}{_own()._format_git_config_value(str(absolute))}{suffix}"
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
    git_dirs = _own()._nested_repository_git_dirs_for_include_scan(
        nested_root,
        containment_roots=containment_roots,
    )
    if git_dirs is None or not git_dirs:
        yield None
        return
    nested_fd = _own()._open_nested_root_directory_fd(nested_root)
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
        roots = _own()._nested_git_metadata_containment_roots(
            _own()._pinned_directory_path(nested_fd),
            containment_roots,
        )
        if roots is None:
            yield None
            return
        opened = _own()._open_nested_probe_git_dir_fds(nested_fd, containment_roots=roots)
        if opened is None:
            yield None
            return
        primary_fd, object_fd = opened
        snap_primary = _own()._snapshot_git_dir_local_configs_via_fd(primary_fd)
        if snap_primary is None:
            yield None
            return
        if object_fd != primary_fd:
            snap_object = _own()._snapshot_git_dir_local_configs_via_fd(object_fd)
            if snap_object is None:
                yield None
                return
        else:
            snap_object = snap_primary
        # HEAD is agent-controlled: use the same bounded O_NOFOLLOW|O_NONBLOCK
        # snapshot as config so a symlink/FIFO/growing file cannot leak foreign
        # contents or hang the monitor (PRRT_kwDOSJAM6s6emN9X).
        head_text = _own()._read_git_dir_child_text_via_fd(primary_fd, "HEAD")
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
        pinned_primary = _own()._pinned_directory_path(primary_fd)
        rewritten_main = _own()._rewrite_relative_core_worktree_for_snapshot(
            main_config, pinned_primary
        )
        if rewritten_main is None:
            yield None
            return
        main_config = rewritten_main
        if worktree_config is not None:
            rewritten_wt = _own()._rewrite_relative_core_worktree_for_snapshot(
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
        if _own()._git_dir_declares_object_alternates(object_fd):
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
        objects_ok, objects_store_fds = _own()._symlink_nested_probe_objects_store_via_fd(
            object_fd, staging
        )
        if not objects_ok:
            yield None
            return
        refs_ok, refs_store_fds = _own()._symlink_nested_probe_refs_store_via_fd(object_fd, staging)
        if not refs_ok:
            yield None
            return
        if not _own()._symlink_git_dir_child_via_fd(
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
        if not _own()._symlink_git_dir_child_via_fd(
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
        if not _own()._symlink_split_index_backing_files_via_fd(
            primary_fd, staging, metadata_leaf_fds
        ):
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
