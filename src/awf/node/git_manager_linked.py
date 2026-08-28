"""Linked-worktree path discovery and checkout usability helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path

from awf.node.git_manager_ownership import git_env_for_bare_repository_probe

_GIT_BARE_PROBE_TIMEOUT_SECONDS = 5.0


def linked_worktree_git_dir(worktree_path: Path) -> Path | None:
    """Return the Git metadata directory linked from a worktree's ``.git`` file."""
    git_file = worktree_path / ".git"
    if not git_file.is_file():
        return None
    try:
        content = git_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    prefix = "gitdir: "
    if not content.startswith(prefix):
        return None
    git_dir = Path(content.removeprefix(prefix).strip())
    if not git_dir.is_absolute():
        git_dir = (worktree_path / git_dir).resolve()
    return git_dir


def linked_worktree_path_from_git_dir(linked_git_dir: Path) -> Path:
    """Return the worktree path from Git's linked-worktree back-reference."""
    # Late import: ``git_manager`` loads this module while defining types.
    from awf.node.git_manager import GitOperationError

    metadata_gitdir = linked_git_dir / "gitdir"
    try:
        raw_gitdir = metadata_gitdir.read_text(encoding="utf-8").strip()
        if not raw_gitdir:
            raise GitOperationError(
                operation="worktree.hooks_path_probe",
                returncode=1,
                stdout="",
                stderr=f"empty linked-worktree gitdir back-reference at {metadata_gitdir}",
                reason_code="MIRROR_HOOKS_PATH_REPAIR_FAILED",
            )
        git_file = Path(raw_gitdir)
        if not git_file.is_absolute():
            git_file = linked_git_dir / git_file
        return git_file.resolve().parent
    except FileNotFoundError as exc:
        # The back-reference file is gone (ENOENT): the linked worktree was
        # removed out from under us, i.e. genuinely stale metadata that
        # ``git worktree prune`` can safely clear.
        raise GitOperationError(
            operation="worktree.hooks_path_probe",
            returncode=1,
            stdout="",
            stderr=f"cannot read linked-worktree gitdir back-reference at {metadata_gitdir}",
            reason_code="MIRROR_HOOKS_PATH_REPAIR_FAILED",
        ) from exc
    except OSError as exc:
        # The back-reference exists but is unreadable (e.g. permission denied):
        # this is a live worktree we merely cannot inspect, not stale metadata.
        # Surface a non-stale error so repair fails closed instead of pruning —
        # ``git worktree prune`` would delete the live worktree's admin files.
        raise GitOperationError(
            operation="worktree.hooks_path_probe",
            returncode=1,
            stdout="",
            stderr=f"cannot access linked-worktree gitdir back-reference at {metadata_gitdir}",
            reason_code="MIRROR_HOOKS_PATH_REPAIR_FAILED",
        ) from exc
    except RuntimeError as exc:
        raise GitOperationError(
            operation="worktree.hooks_path_probe",
            returncode=1,
            stdout="",
            stderr=f"cannot resolve linked-worktree gitdir back-reference at {metadata_gitdir}",
            reason_code="MIRROR_HOOKS_PATH_REPAIR_FAILED",
        ) from exc


def _worktree_checkout_is_usable(worktree_path: Path) -> bool:
    """Return whether ``worktree_path`` looks like a usable managed checkout.

    For linked worktrees, the ``.git`` file may still parse after the mirror-side
    admin directory is gone. Require the linked Git dir to exist and to
    reciprocally register this checkout before treating the path as usable.
    """
    from awf.node.git_manager import GitOperationError

    if not worktree_path.is_dir():
        return False
    git_marker = worktree_path / ".git"
    if git_marker.is_file():
        linked_git_dir = linked_worktree_git_dir(worktree_path)
        if linked_git_dir is None or not linked_git_dir.is_dir():
            return False
        try:
            registered = linked_worktree_path_from_git_dir(linked_git_dir)
            return registered.resolve() == worktree_path.resolve()
        except (GitOperationError, OSError):
            return False
    return git_marker.is_dir()


def _is_stale_linked_worktree_metadata_error(exc: object) -> bool:
    from awf.node.git_manager import GitOperationError

    if not isinstance(exc, GitOperationError):
        return False
    return exc.operation == "worktree.hooks_path_probe" and exc.stderr.startswith(
        "cannot read linked-worktree gitdir back-reference at "
    )


def mirror_path_for_worktree(worktree_path: Path) -> Path | None:
    """Return the bare mirror path backing a linked worktree, when discoverable."""
    linked_git_dir = linked_worktree_git_dir(worktree_path)
    if linked_git_dir is None:
        return None
    commondir = linked_git_dir / "commondir"
    if commondir.is_file():
        try:
            raw = commondir.read_text(encoding="utf-8").strip()
        except OSError:
            raw = ""
        if raw:
            common = Path(raw)
            if not common.is_absolute():
                common = linked_git_dir / common
            return common.resolve()
    return linked_git_dir.parent.parent.resolve()


def _path_within_root(path: Path, root: Path) -> Path | None:
    """Return ``path`` resolved under ``root``, or ``None`` when it escapes ``root``."""
    try:
        resolved_path = path.resolve()
        resolved_root = root.resolve()
    except RuntimeError:
        resolved_path = path.absolute()
        resolved_root = root.absolute()
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError:
        return None
    return resolved_path


def _is_bare_registered_mirror_candidate(mirror_path: Path) -> bool:
    """Return whether ``mirror_path`` probes as a bare Git repository."""
    from awf.node.git_manager import GitOperationError

    try:
        probe = subprocess.run(
            [
                "git",
                "--bare",
                "--git-dir",
                str(mirror_path),
                "rev-parse",
                "--is-bare-repository",
            ],
            capture_output=True,
            check=False,
            env=git_env_for_bare_repository_probe(),
            text=True,
            timeout=_GIT_BARE_PROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return False
    except OSError as exc:
        raise GitOperationError(
            operation="mirror_registry_scan",
            returncode=1,
            stdout="",
            stderr=f"could not probe bare mirror {mirror_path}: {exc}",
            reason_code="MIRROR_REGISTRY_SCAN_FAILED",
        ) from exc
    return probe.returncode == 0 and probe.stdout.strip() == "true"


def mirror_path_for_registered_worktree(worktree_path: Path, mirrors_dir: Path) -> Path | None:
    """Return the bare mirror path from mirror-side linked-worktree metadata.

    This is the fallback for an AWF-managed worktree directory whose ``.git``
    file was already removed, but whose bare mirror still has
    ``worktrees/<id>/gitdir`` pointing back to that directory.
    """
    from awf.node.git_manager import GitOperationError

    if not mirrors_dir.exists():
        return None
    try:
        mirror_paths = sorted(
            resolved_path
            for path in mirrors_dir.iterdir()
            if path.is_dir()
            if (resolved_path := _path_within_root(path, mirrors_dir)) is not None
        )
    except OSError as exc:
        raise GitOperationError(
            operation="mirror_registry_scan",
            returncode=1,
            stdout="",
            stderr=str(exc),
            reason_code="MIRROR_REGISTRY_SCAN_FAILED",
        ) from exc

    worktree_name = worktree_path.name
    try:
        resolved_worktree = worktree_path.resolve()
    except OSError as exc:
        raise GitOperationError(
            operation="mirror_registry_scan",
            returncode=1,
            stdout="",
            stderr=f"cannot resolve worktree path {worktree_path}: {exc}",
            reason_code="MIRROR_REGISTRY_SCAN_FAILED",
        ) from exc
    except RuntimeError:
        resolved_worktree = worktree_path.absolute()
    best_match: tuple[int, Path] | None = None
    for mirror_path in mirror_paths:
        linked_git_dir = mirror_path / "worktrees" / worktree_name
        if not linked_git_dir.is_dir():
            continue
        if not _is_bare_registered_mirror_candidate(mirror_path):
            continue
        try:
            registered_worktree = linked_worktree_path_from_git_dir(linked_git_dir)
        except GitOperationError:
            continue
        if registered_worktree == resolved_worktree:
            try:
                registered_mtime_ns = linked_git_dir.stat().st_mtime_ns
            except OSError:
                registered_mtime_ns = 0
            if best_match is None or registered_mtime_ns >= best_match[0]:
                best_match = (registered_mtime_ns, mirror_path)
    if best_match is not None:
        return best_match[1]
    return None
