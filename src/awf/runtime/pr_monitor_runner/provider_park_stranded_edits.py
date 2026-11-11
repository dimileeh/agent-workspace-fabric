"""Commit the edits a managed-key park stranded before its resume repairs anything.

A review repair stopped by a managed-key park takes the #932 preserve path, and
when that preserving dirty sink fails the park still parks — carrying the sink
failure as ``dirty_commit_error`` — instead of failing the workspace
(PRRT_kwDOSJAM6s618cN-). The agent's edits stay uncommitted in the repair
worktree, and raising the budget resumes the monitor through an operator hint
whose repair-start guard rejects exactly that dirt as
``PRE_EXISTING_DIRTY_WORKTREE``, so the resume could never continue the repair.

The park therefore also leaves a durable marker in the monitor state, and the
resumed hint cycle finishes the failed sink before its guard runs
(PRRT_kwDOSJAM6s618pY4): the stranded edits are committed on top of the
preserved HEAD and the repair carries on, or — when they still will not commit —
the resume fails as the ``REPAIR_DIRTY_COMMIT_FAILED`` the park recorded, never
as an unrelated pre-existing-dirty worktree. Never a rollback: the edits are only
ever committed or left where they are.

The marker leaves the workspace row before the resumed repair's agent runs
(PRRT_kwDOSJAM6s619LVg): the hint cycle only persists its state after the agent
returns, so a marker dropped in memory alone would reload on a restart mid-agent,
and the next resume would commit the agent's leftover files as the park's
residue instead of failing closed on them as pre-existing dirt.

The resume commits only what the park left (PRRT_kwDOSJAM6s61_WVy): the run
loop's park sink records each path the park leaves dirty with its content, and
the resume, under the worktree writer lock, commits only while every dirty path
still matches that evidence. A file someone added or changed while the workspace
sat parked — or a park with no evidence — is left for a human as pre-existing
dirt instead of riding into the repair commit.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, cast

from awf.common.logging import get_logger
from awf.db.repositories import WorkspaceRepository
from awf.runtime.pr_monitor_runner.constants import (
    _PRE_EXISTING_DIRTY_WORKTREE_REASON,
    _REPAIR_DIRTY_COMMIT_FAILED_REASON,
)
from awf.runtime.worktree_writer_lock import hold_exclusive_worktree_writer_lock

if TYPE_CHECKING:
    from awf.runtime.pr_monitor import MonitorState
    from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult

PROVIDER_PARK_STRANDED_EDITS_STATE_KEY: Final = "__awf_provider_park_stranded_edits__"
"""Reserved ``MonitorState.threads_addressed_ids`` key: a park left uncommitted edits.

Its value is the HEAD the park preserved (empty when it could not be read); it
reaches the workspace row through the run loop's ``_persist_state`` ahead of the
park itself, and the operator-hint resume keeps the row's map intact until it
drops the key from the row itself.
"""

PROVIDER_PARK_STRANDED_EDITS_EVIDENCE_STATE_KEY: Final = (
    "__awf_provider_park_stranded_edits_evidence__"
)
"""Reserved ``MonitorState.threads_addressed_ids`` key: what the park left dirty.

A JSON object mapping each dirty path to its content identity, recorded by the
run loop's park sink before it persists the parked state. It lives and leaves
with the stranded-edits marker.
"""

_RESUME_COMMIT_MESSAGE: Final = "fix: commit repair edits stranded by a managed provider key park"

_log = get_logger(__name__)


def mark_provider_park_stranded_edits(
    state: MonitorState | None, preserved_head: str | None
) -> None:
    """Record that a park left uncommitted repair edits for the resumed hint cycle.

    Evidence from an earlier park is dropped: the run loop's park sink records
    this park's (``record_provider_park_stranded_edits_evidence``).
    """
    if state is not None:
        state.threads_addressed_ids[PROVIDER_PARK_STRANDED_EDITS_STATE_KEY] = preserved_head or ""
        state.threads_addressed_ids.pop(PROVIDER_PARK_STRANDED_EDITS_EVIDENCE_STATE_KEY, None)


async def record_provider_park_stranded_edits_evidence(
    runner: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    state: MonitorState,
) -> None:
    """Record what a marked park leaves dirty, before the parked state is persisted.

    The paths are the ones the repair-start guard reports — the ones the resumed
    sink would stage — each with its content identity. A worktree the guard or the
    hashing cannot read records nothing, and the resume then fails closed on any
    dirt it finds.
    """
    threads_addressed = state.threads_addressed_ids
    if (
        PROVIDER_PARK_STRANDED_EDITS_STATE_KEY not in threads_addressed
        or PROVIDER_PARK_STRANDED_EDITS_EVIDENCE_STATE_KEY in threads_addressed
    ):
        return
    try:
        dirty = await runner._pre_existing_dirty_repair_worktree_result(
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            operation_type="provider_park_evidence",
        )
        if dirty is not None and dirty.reason_code != _PRE_EXISTING_DIRTY_WORKTREE_REASON:
            _log.warning(
                "monitor.provider_park_stranded_edits_evidence_unavailable",
                workspace_id=workspace_id,
                reason_code=dirty.reason_code,
            )
            return
        paths = _dirty_paths(dirty)
        identities = await asyncio.to_thread(_path_identities, worktree_path, paths)
    except OSError as exc:
        _log.warning(
            "monitor.provider_park_stranded_edits_evidence_unavailable",
            workspace_id=workspace_id,
            exc_type=type(exc).__name__,
        )
        return
    threads_addressed[PROVIDER_PARK_STRANDED_EDITS_EVIDENCE_STATE_KEY] = json.dumps(
        identities, sort_keys=True
    )


async def commit_provider_park_stranded_edits(
    runner: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    state: MonitorState,
    compose_project: str,
    compose_file: Path,
    remote_branch: str,
    remote_push_url: str | None,
    operation_type: str,
) -> _GitPushResult | None:
    """Finish a park's failed dirty sink; ``None`` lets the resumed repair start.

    The marker is dropped once the worktree is clean — the retried sink committed
    the edits, or something already had — from the workspace row as well as from
    memory, before the resumed repair's agent can dirty the worktree again. Edits
    that still will not commit return
    ``REPAIR_DIRTY_COMMIT_FAILED`` with the stranded paths, and a ``git status``
    failure is returned as-is, exactly as the repair-start guard returns it. Dirt
    the park's evidence does not vouch for is never committed: it returns
    ``PRE_EXISTING_DIRTY_WORKTREE`` naming the unverified paths and keeps the
    marker. Sink exceptions propagate: the caller handles them as it handles the
    same sink raising inside the repair agent's run.
    """
    preserved_head = state.threads_addressed_ids.get(PROVIDER_PARK_STRANDED_EDITS_STATE_KEY)
    if preserved_head is None:
        return None
    # The writer lock spans the evidence check and the commit, so no other AWF
    # writer can change the worktree between the two.
    async with hold_exclusive_worktree_writer_lock(worktree_path):
        dirty = await runner._pre_existing_dirty_repair_worktree_result(
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            operation_type=operation_type,
        )
        if dirty is not None and dirty.reason_code == _PRE_EXISTING_DIRTY_WORKTREE_REASON:
            unverified_paths = await _unverified_stranded_paths(
                state, worktree_path=worktree_path, paths=_dirty_paths(dirty)
            )
            if unverified_paths:
                return _unverified_stranded_edits_result(
                    dirty,
                    workspace_id=workspace_id,
                    preserved_head=preserved_head,
                    unverified_paths=unverified_paths,
                )
            await runner._commit_dirty_worktree(
                workspace_id=workspace_id,
                message=_RESUME_COMMIT_MESSAGE,
                compose_project=compose_project,
                compose_file=compose_file,
                state=state,
                protected_scope_revert_remote_branch=remote_branch,
                remote_push_url=remote_push_url,
                operation_start_head=preserved_head or None,
            )
            dirty = await runner._pre_existing_dirty_repair_worktree_result(
                workspace_id=workspace_id,
                worktree_path=worktree_path,
                operation_type=operation_type,
            )
    if dirty is None:
        # Row first: a write fault then leaves the in-memory marker for the next
        # in-process resume to retry, rather than a row-only one a restart reloads.
        await _drop_marker_from_workspace_row(runner, workspace_id=workspace_id)
        state.threads_addressed_ids.pop(PROVIDER_PARK_STRANDED_EDITS_STATE_KEY, None)
        state.threads_addressed_ids.pop(PROVIDER_PARK_STRANDED_EDITS_EVIDENCE_STATE_KEY, None)
        _log.info(
            "monitor.provider_park_stranded_edits_committed",
            workspace_id=workspace_id,
            preserved_head=preserved_head or None,
        )
        return None
    if dirty.reason_code != _PRE_EXISTING_DIRTY_WORKTREE_REASON:
        return cast("_GitPushResult", dirty)
    from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult

    details = dict(dirty.details or {})
    details.update(phase="provider_park_resume", preserved_head=preserved_head or None)
    _log.warning(
        "monitor.provider_park_stranded_edits_commit_failed",
        workspace_id=workspace_id,
        reason_code=_REPAIR_DIRTY_COMMIT_FAILED_REASON,
        preserved_head=preserved_head or None,
        paths=details.get("paths"),
    )
    return _GitPushResult(
        pushed=False,
        failed=True,
        returncode=1,
        stderr=(
            "A managed provider key park left uncommitted repair edits and the resumed "
            "dirty-worktree sink could not commit them; they are stranded in the repair "
            "worktree."
        ),
        reason_code=_REPAIR_DIRTY_COMMIT_FAILED_REASON,
        details=details,
    )


def _dirty_paths(dirty: _GitPushResult | None) -> list[str]:
    paths = (dirty.details or {}).get("paths") if dirty is not None else None
    return [str(path) for path in paths] if isinstance(paths, list) else []


def _path_identities(worktree_path: Path, paths: list[str]) -> dict[str, str | None]:
    return {path: _path_identity(worktree_path / path) for path in paths}


def _path_identity(path: Path) -> str | None:
    """What ``git add`` would stage from ``path``; ``None`` when that is unverifiable.

    A directory (a nested repository or submodule) or any other special file has no
    content identity here, so it never matches and is always left for a human.
    """
    try:
        info = path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return "absent"
    if stat.S_ISLNK(info.st_mode):
        return "symlink:" + hashlib.sha256(os.fsencode(path.readlink())).hexdigest()
    if not stat.S_ISREG(info.st_mode):
        return None
    with path.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    return f"file:{'x' if info.st_mode & 0o111 else '-'}:{digest}"


async def _unverified_stranded_paths(
    state: MonitorState, *, worktree_path: Path, paths: list[str]
) -> list[str]:
    """Return the dirty paths the park's evidence does not vouch for."""
    try:
        evidence = json.loads(
            state.threads_addressed_ids.get(PROVIDER_PARK_STRANDED_EDITS_EVIDENCE_STATE_KEY) or ""
        )
        current = await asyncio.to_thread(_path_identities, worktree_path, paths)
    except (ValueError, OSError):
        return sorted(paths)
    if not isinstance(evidence, dict):
        return sorted(paths)
    return sorted(
        path for path in paths if current[path] is None or evidence.get(path) != current[path]
    )


def _unverified_stranded_edits_result(
    dirty: _GitPushResult,
    *,
    workspace_id: str,
    preserved_head: str,
    unverified_paths: list[str],
) -> _GitPushResult:
    from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult

    details = dict(dirty.details or {})
    details.update(
        phase="provider_park_resume",
        preserved_head=preserved_head or None,
        unverified_paths=unverified_paths,
    )
    _log.warning(
        "monitor.provider_park_stranded_edits_unverified",
        workspace_id=workspace_id,
        reason_code=_PRE_EXISTING_DIRTY_WORKTREE_REASON,
        preserved_head=preserved_head or None,
        unverified_paths=unverified_paths,
    )
    return _GitPushResult(
        pushed=False,
        failed=True,
        returncode=1,
        stderr=(
            "A managed provider key park left uncommitted repair edits, but the repair "
            "worktree no longer holds only those edits; refusing to commit changes the "
            "park did not leave."
        ),
        reason_code=_PRE_EXISTING_DIRTY_WORKTREE_REASON,
        details=details,
    )


async def _drop_marker_from_workspace_row(runner: Any, *, workspace_id: str) -> None:
    """Remove ONLY the stranded-edits marker (and its evidence) from the persisted row.

    Never the whole ``MonitorState``, which inside a fix cycle can still carry
    unconfirmed verdicts (#305) — the single-key shape
    ``_clear_preserved_head_marker_durably`` uses. A write fault propagates: the
    resume then stops before its agent runs, and the next one finds a clean
    worktree and drops the marker again.
    """
    session_factory = getattr(getattr(runner, "_deps", None), "session_factory", None)
    if not callable(session_factory):
        return
    async with session_factory() as session:
        ws = await WorkspaceRepository(session).get_for_update(workspace_id)
        if ws is None:
            return
        threads_addressed = dict(ws.monitor_threads_addressed or {})
        threads_addressed.pop(PROVIDER_PARK_STRANDED_EDITS_STATE_KEY, None)
        threads_addressed.pop(PROVIDER_PARK_STRANDED_EDITS_EVIDENCE_STATE_KEY, None)
        ws.monitor_threads_addressed = threads_addressed
        await session.commit()
