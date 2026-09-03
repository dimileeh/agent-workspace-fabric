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
    """
    Open the nested root directory without following symlinks.
    
    Returns:
        int | None: An open directory file descriptor, or `None` if the path cannot
            be opened as a directory.
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
    """
    Open a relative directory path from an existing directory file descriptor without following symlinks.
    
    Parameters:
    	dir_fd (int): File descriptor for the starting directory.
    	relative (Path): Relative path to traverse.
    
    Returns:
    	int | None: File descriptor for the target directory, or `None` if the path is absolute or cannot be opened safely.
    """
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
    """
    Open a directory beneath an approved containment root without following symlinks.
    
    Parameters:
        probe (Path): Directory to open.
        containment_roots (Sequence[Path]): Roots within which the directory must be located.
    
    Returns:
        int | None: An owned file descriptor for the directory, or `None` if it cannot be securely opened.
    """
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
    """
    Open a Git metadata directory candidate only when it is within an approved containment root.
    
    Parameters:
    	candidate (Path): Relative or absolute path to the metadata directory.
    	base_fd (int): File descriptor used to resolve relative candidates.
    	containment_roots (Sequence[Path]): Directories within which the candidate must reside.
    
    Returns:
    	int | None: A retained file descriptor for the candidate, or `None` when it is invalid, inaccessible, or outside the approved roots.
    """
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
    """
    Determine whether the Git object store declares an alternate object location.
    
    Parameters:
        object_fd (int): File descriptor for the Git directory.
    
    Returns:
        bool: `true` if an alternates declaration exists or probing the relevant
        metadata fails, `false` if the metadata is absent and accessible.
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
    """
    Materialize an object-store directory tree while avoiding symlinked directories.
    
    Regular-file leaves are staged securely, while symlinks and unsupported entries cause
    the operation to fail. Enumeration is limited by the shared entry, depth, and time
    budgets.
    
    Parameters:
    	dir_fd (int): File descriptor for the source directory.
    	staging_dir (Path): Destination directory for the materialized tree.
    	held_fds (list[int]): Collection retaining file descriptors for staged leaves.
    	skip_names (frozenset[str]): Entry names to omit.
    	budget (_ObjectStoreEnumBudget | None): Shared enumeration limits.
    	depth (int): Current recursion depth.
    
    Returns:
    	bool: `True` if the tree is materialized successfully, `False` if an unsafe entry,
    	unsupported entry, enumeration error, or budget limit is encountered.
    """
    if budget is None:
        budget = _own()._ObjectStoreEnumBudget(
            entries_remaining=_own()._OBJECT_STORE_ENUM_AGGREGATE_MAX_ENTRIES,
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
                    if not _own()._symlink_git_dir_child_via_fd(
                        dir_fd,
                        name,
                        staging_dir / name,
                        held_fds,
                        expect_directory=False,
                        validate_git_loose_object=True,
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
    """
    Materialize a Git object store in a staging directory without including its
    live ``info`` content.
    
    Parameters:
    	object_fd (int): File descriptor for the Git directory containing
    		``objects``.
    	staging (Path): Directory where the staged ``objects`` tree is created.
    
    Returns:
    	tuple[bool, list[int]]: A success flag and any file descriptors retained
    		during materialization. The descriptor list is empty after cleanup.
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
    """
    Materialize the Git refs store in a staging directory.
    
    Parameters:
    	object_fd (int): File descriptor for the Git objects directory.
    	staging (Path): Directory in which to create the staged refs store.
    
    Returns:
    	tuple[bool, list[int]]: Whether materialization succeeded and any file descriptors retained during the operation.
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
    """
    Decode a Git configuration value, including quoted values and trailing comments.
    
    Parameters:
        raw (str): The raw configuration value.
    
    Returns:
        str: The decoded configuration value.
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
    """
    Format a Git configuration value with quoting and escaping when required.
    
    Parameters:
    	value (str): The configuration value to format.
    
    Returns:
    	str: The value formatted for use in a Git configuration file.
    """
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
    """
    Rewrite relative ``core.worktree`` values as absolute paths based on the original Git directory.
    
    Parameters:
        text (str): Git configuration text to rewrite.
        original_git_dir (Path): Git directory used to resolve relative worktree paths.
    
    Returns:
        str | None: The rewritten configuration text, or ``None`` if a relative path cannot be resolved.
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
    """
    Create a private snapshot of a nested repository's Git metadata for probing.
    
    Parameters:
    	nested_root (Path): Root directory of the nested repository.
    	containment_roots (Sequence[Path] | None): Optional directories that Git metadata must remain within.
    
    Yields:
    	Path | None: The temporary Git-directory snapshot, or `None` when the repository metadata is unsafe or cannot be materialized.
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
