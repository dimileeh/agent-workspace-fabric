"""Validation worktree Git probes: symlink form, executable-bit capability, timeouts.

Split out of ``validation_worktree`` to keep it under the first-party line budget.
``validation_worktree`` re-exports every public/private name defined here, so
callers and tests keep importing from it; patches on helpers that these probes
call internally must target this module.
"""

from __future__ import annotations

import contextlib
import errno
import inspect
import os
import secrets
import stat
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path, PurePosixPath
from typing import Any

from awf.common.commands import CommandResult
from awf.node.git_manager import (
    FORCE_FILE_MODE_TRACKING_GIT_CONFIG_ARGS,
    FORCE_SYMLINK_TRACKING_GIT_CONFIG_ARGS,
)
from awf.runtime.validation_worktree_constants import (
    VALIDATION_WORKTREE_GIT_TIMEOUT_SECONDS as _VALIDATION_WORKTREE_GIT_TIMEOUT_SECONDS,
)
from awf.runtime.validation_worktree_constants import (
    VALIDATION_WORKTREE_STATUS_FAILED,
)
from awf.runtime.validation_worktree_types import ValidationWorktreeCheck

GitRunner = Callable[..., Awaitable[CommandResult]]


async def _run_validation_git(run_git: GitRunner, args: list[str]) -> CommandResult:
    """Invoke ``run_git`` with a finite timeout when the runner supports it."""
    kwargs: dict[str, Any] = {}
    try:
        parameters = inspect.signature(run_git).parameters
    except (TypeError, ValueError):
        parameters = None
    accepts_timeout = parameters is not None and (
        "timeout_seconds" in parameters
        or any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())
    )
    if accepts_timeout:
        kwargs["timeout_seconds"] = _VALIDATION_WORKTREE_GIT_TIMEOUT_SECONDS
    if kwargs:
        return await run_git(args, **kwargs)
    return await run_git(args)


class _CoreSymlinksProbeError(Exception):
    """Raised when ``core.symlinks`` cannot be read for validation cleanliness."""

    def __init__(self, message: str, *, stderr: str | None = None) -> None:
        super().__init__(message)
        self.stderr = stderr


_GIT_INDEX_SYMLINK_MODE = "120000"


async def _core_symlinks_enabled(run_git: GitRunner) -> bool:
    """Return whether the worktree currently checks out index symlinks as symlinks.

    An absent setting (``git config --get`` exit 1) is the enabled default.
    Timeouts and other operational failures raise ``_CoreSymlinksProbeError``
    so callers fail the cleanliness check closed rather than treating the
    failure as enabled and omitting ``-c core.symlinks=true``
    (PRRT_kwDOSJAM6s6fIJuB).
    """
    result = await _run_validation_git(
        run_git,
        ["config", "--no-includes", "--bool", "--get", "core.symlinks"],
    )
    if result.returncode == 1:
        return True
    if not result.ok:
        raise _CoreSymlinksProbeError(
            "Could not read `core.symlinks` for validation worktree cleanliness.",
            stderr=result.stderr or "",
        )
    return result.stdout.strip().lower() != "false"


def _worktree_filesystem_supports_symlinks(worktree_path: Path) -> bool | None:
    """Return whether ``worktree_path`` can materialize real symlinks.

    Used for empty-index baselines so capability is per-worktree filesystem
    state, not shared agent-writable ``core.symlinks`` config
    (PRRT_kwDOSJAM6s6fA_x2).

    After a successful create, probe removal must succeed before reporting
    capability: suppressing unlink errors would leave ``.awf-symlink-cap-*``
    untracked for a later ``git add -A`` (PRRT_kwDOSJAM6s6fBSST).

    Create/``is_symlink`` failures must not be treated as demonstrated lack of
    symlink support: returning False would persist a placeholder baseline and
    let an agent hide symlink→file typechanges after restoring write access
    (PRRT_kwDOSJAM6s6fGb8R). Return ``None`` (indeterminate) instead; only a
    successful create that is not a real symlink may return False.
    """
    probe = worktree_path / f".awf-symlink-cap-{secrets.token_hex(8)}"
    try:
        probe.symlink_to("awf-symlink-cap-target")
        capable = probe.is_symlink()
    except OSError:
        with contextlib.suppress(OSError):
            probe.unlink(missing_ok=True)
        return None
    probe.unlink(missing_ok=True)
    return capable


def _index_symlink_paths_from_ls_files_z(stdout: str) -> tuple[str, ...]:
    """Return tracked index symlink paths from ``git ls-files -s -z`` output."""
    if not stdout:
        return ()
    parts = stdout.split("\0")
    if parts and parts[-1] == "":
        parts = parts[:-1]
    paths: list[str] = []
    for entry in parts:
        if not entry:
            continue
        meta, _, path = entry.partition("\t")
        if not path:
            continue
        mode = meta.split(" ", 1)[0] if meta else ""
        if mode != _GIT_INDEX_SYMLINK_MODE:
            continue
        paths.append(path)
    return tuple(dict.fromkeys(paths))


async def _index_symlink_paths(run_git: GitRunner) -> tuple[str, ...] | None:
    """Return tracked index paths currently staged as symlinks (mode ``120000``).

    Returns ``None`` when ``git ls-files`` fails or times out so callers do not
    treat command failure as proof the index has no symlink entries
    (PRRT_kwDOSJAM6s6fBSSK).
    """
    listed = await _run_validation_git(run_git, ["ls-files", "-s", "-z"])
    if not listed.ok:
        return None
    # Prefer raw bytes: AsyncioSubprocessRunner replacement-decodes ``stdout``,
    # which turns invalid UTF-8 pathnames into ``�`` and breaks on-disk probes
    # for the symlink-form baseline (PRRT_kwDOSJAM6s6fBSSD).
    if listed.stdout_bytes is not None:
        stdout = listed.stdout_bytes.decode("utf-8", errors="surrogateescape")
    else:
        stdout = listed.stdout or ""
    return _index_symlink_paths_from_ls_files_z(stdout)


async def read_validation_worktree_symlink_form_baseline(
    run_git: GitRunner,
    worktree_path: Path,
) -> bool | None:
    """Return whether this checkout materializes index symlinks as symlinks.

    Call only while the worktree is still agent-immutable (pre-agent capture).
    Post-agent callers must reuse the persisted ``Workspace.block_index_symlinks_
    are_symlinks`` value instead of re-reading mutable paths.

    When the index already tracks symlinks, probe on-disk form. When the index
    has no symlink entries yet, persist per-worktree filesystem symlink
    capability instead of shared ``core.symlinks``: linked worktrees share bare
    mirror config, so an agent-writable false value would poison sibling
    baselines and suppress forced symlink tracking (PRRT_kwDOSJAM6s6fA_x2,
    PRRT_kwDOSJAM6s6e-Zcu).

    When the index symlink listing fails, return ``None`` (indeterminate)
    rather than assuming an empty index and recording filesystem capability
    (PRRT_kwDOSJAM6s6fBSSK). When the empty-index filesystem probe errors,
    likewise return ``None`` rather than persisting False
    (PRRT_kwDOSJAM6s6fGb8R).

    Index symlink paths must agree on on-disk form. ``any(...)`` would mark a
    mixed placeholder+symlink checkout as materialized, forcing
    ``core.symlinks=true`` and reporting every unchanged placeholder as a
    typechange so the tree becomes permanently unvalidatable
    (PRRT_kwDOSJAM6s6fIJuG). Collapsing mixed forms to ``False`` disables forced
    tracking for every path, so replacing a remaining real symlink with an
    equal-target regular file stays hidden under ``core.symlinks=false``
    (PRRT_kwDOSJAM6s6fK4k2). Require unanimous symlink form for True, unanimous
    placeholders for False, and return ``None`` (fail closed) when forms mix —
    Git's ``core.symlinks`` is global, so a single bool cannot protect both.
    """
    index_symlink_paths = await _index_symlink_paths(run_git)
    if index_symlink_paths is None:
        return None
    if not index_symlink_paths:
        return _worktree_filesystem_supports_symlinks(worktree_path)
    # Unanimous forms only. Mixed → None (fail closed): True dirties placeholders
    # (PRRT_kwDOSJAM6s6fIJuG); False hides real-symlink rematerialization
    # (PRRT_kwDOSJAM6s6fK4k2).
    forms = tuple((worktree_path / relative).is_symlink() for relative in index_symlink_paths)
    if all(forms):
        return True
    if not any(forms):
        return False
    return None


async def _symlink_tracking_git_config_args(
    run_git: GitRunner,
    *,
    trusted_index_symlinks_are_symlinks: bool | None,
) -> tuple[str, ...]:
    """Return ``-c core.symlinks=true`` only when it un-hides agent tampering.

    Checkouts that legitimately set ``core.symlinks=false`` represent index
    symlinks as plain-file placeholders; forcing ``core.symlinks=true`` then
    reports clean placeholders as typechanges and ``restore`` can mutate the
    tree (PRRT_kwDOSJAM6s6e8u_0). Only override when validation started with
    on-disk symlinks but the agent later flipped ``core.symlinks=false`` to
    hide a symlink→file typechange (PRRT_kwDOSJAM6s6ezrHU).

    Placeholder baselines intentionally omit the force; callers must still
    reject equal-target rematerialization via
    ``_placeholder_baseline_rematerialized_symlink_paths``
    (PRRT_kwDOSJAM6s6fMRYV).

    Operational failure reading ``core.symlinks`` raises
    ``_CoreSymlinksProbeError`` so callers fail closed instead of omitting the
    forced type-change check (PRRT_kwDOSJAM6s6fIJuB).
    """
    if trusted_index_symlinks_are_symlinks is False:
        return ()
    if await _core_symlinks_enabled(run_git):
        return ()
    return FORCE_SYMLINK_TRACKING_GIT_CONFIG_ARGS


_WORKTREE_DIRECTORY_OPEN_FLAGS = (
    os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
)


def _worktree_relative_path_parts(relative: str) -> tuple[str, ...]:
    """Split a worktree-relative path and reject empty / ``.`` / ``..`` components."""
    parts = PurePosixPath(relative).parts
    if not parts or any(part in {".", ".."} for part in parts):
        raise OSError(errno.EINVAL, "unsafe worktree relative path", relative)
    return parts


@contextlib.contextmanager
def _open_worktree_parent_dir_nofollow(
    worktree_path: Path, relative: str
) -> Iterator[tuple[int, str]]:
    """Yield ``(parent_dir_fd, final_name)`` without following any path component.

    Pathname ``Path.is_symlink`` / ``Path.unlink`` follow intermediate directory
    components, so a parent swapped for a symlink can escape the worktree
    (PRRT_kwDOSJAM6s6fNhYT). Walk each parent with ``O_DIRECTORY|O_NOFOLLOW``.
    """
    parts = _worktree_relative_path_parts(relative)
    dir_fd = os.open(worktree_path, _WORKTREE_DIRECTORY_OPEN_FLAGS)
    try:
        for part in parts[:-1]:
            child_fd = os.open(part, _WORKTREE_DIRECTORY_OPEN_FLAGS, dir_fd=dir_fd)
            os.close(dir_fd)
            dir_fd = child_fd
        yield dir_fd, parts[-1]
    finally:
        os.close(dir_fd)


def _worktree_entry_is_symlink_nofollow(worktree_path: Path, relative: str) -> bool | None:
    """Return whether ``relative`` is a symlink under ``worktree_path``.

    Never follows intermediate directory symlinks. Returns ``None`` when the
    path cannot be probed safely (parent symlink, permission error, unsafe
    relative) so callers fail closed rather than classifying outside-worktree
    state (PRRT_kwDOSJAM6s6fNhYT). Missing final entries are ``False``.
    """
    try:
        with _open_worktree_parent_dir_nofollow(worktree_path, relative) as (dir_fd, name):
            return stat.S_ISLNK(os.lstat(name, dir_fd=dir_fd).st_mode)
    except FileNotFoundError:
        return False
    except OSError as exc:
        if exc.errno in {errno.ENOENT, errno.ENOTDIR}:
            # Final component missing, or a non-directory blocks the walk in a
            # way that proves the indexed leaf is not present in-tree.
            return False
        return None


def _unlink_worktree_symlink_nofollow(worktree_path: Path, relative: str) -> None:
    """Unlink ``relative`` when it is a symlink, without following parent links.

    No-op when the final entry exists and is not a symlink. Raises ``OSError``
    when the no-follow walk fails or unlink fails so cleanup fails closed
    instead of deleting through a parent directory symlink
    (PRRT_kwDOSJAM6s6fNhYT).
    """
    with _open_worktree_parent_dir_nofollow(worktree_path, relative) as (dir_fd, name):
        try:
            mode = os.lstat(name, dir_fd=dir_fd).st_mode
        except FileNotFoundError:
            return
        if not stat.S_ISLNK(mode):
            return
        os.unlink(name, dir_fd=dir_fd)


async def _placeholder_baseline_rematerialized_symlink_paths(
    run_git: GitRunner,
    worktree_path: Path,
) -> tuple[str, ...] | None:
    """Return index symlink paths rematerialized as real symlinks on disk.

    When the trusted baseline was placeholders (``False``), Git with
    ``core.symlinks=false`` reports equal-target placeholder↔symlink as clean.
    Probe on-disk forms directly so validation and cleanup reject the type
    change (PRRT_kwDOSJAM6s6fMRYV).

    Returns ``None`` when the index symlink listing fails so callers fail
    closed rather than treating listing failure as an empty rematerialization
    set. Individual path probes use no-follow descent so a parent directory
    symlink cannot classify (or later unlink) a path outside the worktree
    (PRRT_kwDOSJAM6s6fNhYT); an unsafe probe fails the whole set closed.
    """
    index_symlink_paths = await _index_symlink_paths(run_git)
    if index_symlink_paths is None:
        return None
    rematerialized: list[str] = []
    for relative in index_symlink_paths:
        form = _worktree_entry_is_symlink_nofollow(worktree_path, relative)
        if form is None:
            return None
        if form:
            rematerialized.append(relative)
    return tuple(rematerialized)


def _worktree_filesystem_supports_file_mode(worktree_path: Path) -> bool:
    """Return whether ``worktree_path`` preserves the executable bit.

    Used so validation can honor checkouts that legitimately set
    ``core.fileMode=false`` on filesystems that do not store +x
    (PRRT_kwDOSJAM6s6fFVFP). Prefer per-worktree filesystem state over shared
    agent-writable ``core.fileMode`` config (same reason empty-index symlink
    baselines probe the filesystem — PRRT_kwDOSJAM6s6fA_x2).

    Capability requires both transitions: clear ``+x`` then set it again.
    Only checking ``chmod(0755)`` misclassifies filesystems that always expose
    regular files as executable or ignore chmod while the default mode already
    has ``+x`` — a common reason for ``core.fileMode=false`` — and would force
    mode tracking that dirties unchanged ``100644`` index entries
    (PRRT_kwDOSJAM6s6fF6Nh).

    After a successful create, probe removal must succeed before reporting
    capability: suppressing unlink errors would leave ``.awf-filemode-cap-*``
    untracked for a later cleanliness check (PRRT_kwDOSJAM6s6fBSST).

    Create/chmod/stat failures must not be treated as demonstrated lack of
    mode support: returning False would omit ``-c core.fileMode=true`` and let
    an agent hide +x flips by blocking the probe (for example after setting
    ``core.fileMode=false`` and removing worktree write permission)
    (PRRT_kwDOSJAM6s6fGIft).

    Pin create with ``O_EXCL|O_NOFOLLOW`` and apply ``fchmod``/``fstat`` on the
    retained descriptor so a concurrent rename+symlink swap cannot redirect
    pathname ``chmod`` onto a host path outside the worktree
    (PRRT_kwDOSJAM6s6fGSCT).
    """
    probe = worktree_path / f".awf-filemode-cap-{secrets.token_hex(8)}"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    fd = os.open(probe, flags, 0o644)
    try:
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):  # pragma: no cover - O_EXCL creates a regular file
                raise OSError("file-mode capability probe is not a regular file")
            os.fchmod(fd, 0o644)
            cleared = not bool(os.fstat(fd).st_mode & stat.S_IXUSR)
            os.fchmod(fd, 0o755)
            set_ok = bool(os.fstat(fd).st_mode & stat.S_IXUSR)
            capable = cleared and set_ok
        finally:
            os.close(fd)
    except OSError:
        with contextlib.suppress(OSError):
            probe.unlink(missing_ok=True)
        raise
    probe.unlink(missing_ok=True)
    return capable


def _file_mode_tracking_git_config_args(
    worktree_path: Path,
    *,
    trusted_file_mode_honored: bool | None,
) -> tuple[str, ...]:
    """Return ``-c core.fileMode=true`` only when executable bits are honored.

    Checkouts that legitimately set ``core.fileMode=false`` because the
    filesystem cannot preserve +x report clean mode mismatches under that
    setting; forcing ``core.fileMode=true`` then marks every such path dirty
    and restore/reset may rewrite modes (PRRT_kwDOSJAM6s6fFVFP). Only force
    when the trusted pre-agent capability (or a live filesystem probe) says
    executable bits are honored, so agent-set ``core.fileMode=false`` on a
    capable checkout still cannot hide +x flips (PRRT_kwDOSJAM6s6ey_47).
    """
    honored = (
        trusted_file_mode_honored
        if trusted_file_mode_honored is not None
        else _worktree_filesystem_supports_file_mode(worktree_path)
    )
    if not honored:
        return ()
    return FORCE_FILE_MODE_TRACKING_GIT_CONFIG_ARGS


def _core_symlinks_probe_failure_check(exc: _CoreSymlinksProbeError) -> ValidationWorktreeCheck:
    """Map ``core.symlinks`` probe failures to a failed cleanliness check."""
    stderr = (exc.stderr or "")[:1000]
    return ValidationWorktreeCheck(
        clean=False,
        reason_code=VALIDATION_WORKTREE_STATUS_FAILED,
        message=str(exc),
        command_stderr=stderr,
    )


def _file_mode_probe_failure_check(exc: OSError) -> ValidationWorktreeCheck:
    """Map file-mode capability probe failures to a failed cleanliness check."""
    detail = str(exc).strip() or exc.__class__.__name__
    return ValidationWorktreeCheck(
        clean=False,
        reason_code=VALIDATION_WORKTREE_STATUS_FAILED,
        message=(
            "Could not probe worktree executable-bit capability for validation "
            f"cleanliness: {detail}"
        ),
    )
