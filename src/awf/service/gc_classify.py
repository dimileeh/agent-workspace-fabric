"""Leaf path/classification helpers for terminal-workspace filesystem GC.

These are pure (or near-pure) helpers extracted from ``awf.service.gc`` to keep
that module under the maintainability line limit. They are re-imported back into
``awf.service.gc`` so every public/private name remains importable from there.
"""

from __future__ import annotations

import errno
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from awf.runtime.inspection import RuntimeService, RuntimeSnapshot

if TYPE_CHECKING:
    from awf.db.models import Workspace
    from awf.service.gc import WorkspaceGCPathOutcome

PATH_DELETED = "PATH_DELETED"
PATH_ALREADY_REMOVED = "PATH_ALREADY_REMOVED"
PATH_DELETE_FAILED = "PATH_DELETE_FAILED"
PATH_DELETE_PERMISSION_DENIED = "PATH_DELETE_PERMISSION_DENIED"

# Deletion is refused (not absent) when the OS denies permission -- e.g. the
# host CLI process (uid 1000) cannot remove a root-owned per-workspace auth dir.
# Such a refusal must never collapse to ``already_removed`` success.
_PERMISSION_DENIED_ERRNOS = frozenset({errno.EACCES, errno.EPERM})

_FAILED_NO_WORK_RUNTIME_IDLE_PATTERNS = ("sleep infinity", "tail -f /dev/null")


@dataclass(frozen=True)
class WorkspaceGCPath:
    """One filesystem target considered for workspace GC."""

    kind: str
    path: Path
    exists: bool
    estimated_bytes: int

    def to_dict(
        self,
        *,
        deleted: bool = False,
        error: str | None = None,
        status: str | None = None,
        reason_code: str | None = None,
    ) -> dict[str, object]:
        resolved_status = status or ("already_removed" if not self.exists else "planned")
        resolved_reason = reason_code or (
            PATH_ALREADY_REMOVED if not self.exists else "PATH_PLANNED"
        )
        payload: dict[str, object] = {
            "path": str(self.path),
            "exists": self.exists,
            "estimated_bytes": self.estimated_bytes,
            "deleted": deleted,
            "status": resolved_status,
            "reason_code": resolved_reason,
        }
        if error is not None:
            payload["error"] = error
        return payload


def _compose_project_name_for_workspace(workspace: Workspace) -> str | None:
    return workspace.compose_project_name or None


def _snapshot_has_no_work(snapshot: RuntimeSnapshot) -> bool:
    if snapshot.stack_state == "unavailable":
        return False
    if not snapshot.services:
        return False

    agent_services = [
        service for service in snapshot.services if (service.name or "").lower() == "agent"
    ]
    if not agent_services:
        return False
    return _agent_service_has_no_work(agent_services[0])


def _agent_service_has_no_work(service: RuntimeService) -> bool:
    if (service.state or "").lower() != "running":
        return False
    return bool(_container_command_is_idle(service.command))


def _container_command_is_idle(command: str | None) -> bool:
    if not command:
        return False
    command_text = command.lower()
    return any(pattern in command_text for pattern in _FAILED_NO_WORK_RUNTIME_IDLE_PATTERNS)


def _has_pr_metadata(workspace: Workspace) -> bool:
    return bool(workspace.pr_url or workspace.pr_number)


def _pr_has_merged(workspace: Workspace) -> bool:
    return workspace.pr_merge_sha is not None


def _path_payload_for_candidate(
    item: WorkspaceGCPath,
    *,
    deleted_paths: set[Path],
    delete_errors: dict[tuple[str, Path], str],
    path_outcomes: dict[tuple[str, Path], WorkspaceGCPathOutcome],
) -> dict[str, object]:
    outcome = path_outcomes.get((item.kind, item.path))
    if outcome is not None:
        return item.to_dict(
            deleted=outcome.deleted,
            error=outcome.error,
            status=outcome.status,
            reason_code=outcome.reason_code,
        )
    error = delete_errors.get((item.kind, item.path))
    return item.to_dict(
        deleted=item.path in deleted_paths,
        error=error,
        status="failed" if error else None,
        reason_code=PATH_DELETE_FAILED if error else None,
    )


def _gc_path(kind: str, path: Path) -> WorkspaceGCPath:
    return WorkspaceGCPath(
        kind=kind,
        path=path,
        exists=path.exists(),
        estimated_bytes=_estimate_bytes(path),
    )


def _estimate_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    for child in path.rglob("*"):
        try:
            if child.is_file():
                total += child.stat().st_size
        except OSError:
            continue
    return total


def _delete_gc_path(
    target: WorkspaceGCPath, *, work_dir: Path
) -> tuple[bool, str | None, str | None]:
    """Delete one pressure dir, returning ``(deleted, error, failure_reason_code)``.

    ``failure_reason_code`` distinguishes a permission refusal
    (``PATH_DELETE_PERMISSION_DENIED``) from any other delete failure
    (``PATH_DELETE_FAILED``) so a root-owned dir the caller cannot remove is
    reported loudly instead of being mistaken for an absent path. It is ``None``
    on success and on the genuine not-exists case (nothing to reclaim).

    The preflight probes (``exists``/``is_symlink``/``is_dir``) share the same
    permission-aware handling as the ``rmtree`` call: ``pathlib`` only swallows
    ``ENOENT``/``ENOTDIR``/``EBADF``/``ELOOP``, so a ``stat`` that fails because
    the process cannot traverse a root-owned ``0700`` parent raises instead of
    returning ``False``. Without this guard such a failure would escape the GC
    run rather than being recorded as ``PATH_DELETE_PERMISSION_DENIED``.
    """
    try:
        if not target.path.exists():
            return False, None, None
        if not _is_safe_gc_path(target, work_dir=work_dir):
            return False, "path is outside the expected service GC roots", PATH_DELETE_FAILED
        if target.path.is_symlink():
            return False, "refusing to delete symlink", PATH_DELETE_FAILED
        if not target.path.is_dir():
            return False, "refusing to delete non-directory path", PATH_DELETE_FAILED
        shutil.rmtree(target.path)
    except PermissionError as exc:
        return False, str(exc), PATH_DELETE_PERMISSION_DENIED
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            # A concurrent GC run removed the dir between the preflight
            # ``exists`` probe and ``rmtree``. That is an idempotent no-op, not
            # a failure: report ``PATH_ALREADY_REMOVED`` so the run stays clean.
            return False, None, PATH_ALREADY_REMOVED
        reason_code = (
            PATH_DELETE_PERMISSION_DENIED
            if exc.errno in _PERMISSION_DENIED_ERRNOS
            else PATH_DELETE_FAILED
        )
        return False, str(exc), reason_code
    return True, None, None


def _is_safe_gc_path(target: WorkspaceGCPath, *, work_dir: Path) -> bool:
    roots = {
        "worktree": work_dir / "git" / "worktrees",
        "companion_worktree": work_dir / "git" / "worktrees",
        "compose": work_dir / "compose",
        "auth": work_dir / "auth",
    }
    root = roots.get(target.kind.split(":", maxsplit=1)[0])
    if root is None:
        return False
    try:
        target.path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return False
    return True
