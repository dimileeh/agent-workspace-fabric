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
the resume, under the worktree writer lock, commits exactly those paths — and
only while every one still holds that content and no other path is dirty. A
file someone added or changed while the workspace sat parked — or a park with no
evidence — is neither committed nor discarded: the resume stays parked for a
human as ``PARK_RESUME_UNEXPECTED_WORKTREE_CHANGES``, naming those paths.

A park marked by the pre-push dirty finalize also records the finalize's
operation-owned delta (PRRT_kwDOSJAM6s63M6rp): the park sink records whatever is
dirty when it parks, and the resume commits it without the finalize's ownership
gate. A recorded path outside that boundary keeps the resume parked the same way,
and the resume commit passes the finalize's committed-delta gate before it can be
published: a committed path outside the boundary fails closed as
``PRE_PUSH_DIRTY_FINALIZE_UNOWNED_DELTA``, an uninspectable delta as
``PRE_PUSH_DIRTY_FINALIZE_DELTA_UNAVAILABLE``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
from collections.abc import Collection
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, cast

from awf.common.logging import get_logger
from awf.common.work_slot import hold_work_slot
from awf.db.repositories import WorkspaceRepository
from awf.runtime.pr_monitor_runner.constants import (
    _PARK_RESUME_UNEXPECTED_WORKTREE_CHANGES_REASON,
    _PRE_EXISTING_DIRTY_WORKTREE_REASON,
    _REPAIR_DIRTY_COMMIT_FAILED_REASON,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_constants import (
    _PRE_PUSH_DIRTY_FINALIZE_DELTA_UNAVAILABLE_REASON,
    _PRE_PUSH_DIRTY_FINALIZE_UNOWNED_DELTA_REASON,
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

A JSON object mapping each dirty repo-relative path to its content identity,
recorded by the run loop's park sink before it persists the parked state
alongside the marker's HEAD. It lives and leaves with the stranded-edits marker.
"""

PROVIDER_PARK_STRANDED_EDITS_OWNED_STATE_KEY: Final = "__awf_provider_park_stranded_edits_owned__"
"""Reserved ``MonitorState.threads_addressed_ids`` key: what the resume may commit.

A JSON list of the operation-owned paths, recorded when the pre-push dirty finalize
marks the park; absent for parks with no ownership boundary. It lives and leaves
with the stranded-edits marker.
"""

_RESUME_COMMIT_MESSAGE: Final = "fix: commit repair edits stranded by a managed provider key park"
_MESSAGE_PATH_LIMIT: Final = 10

_log = get_logger(__name__)


def mark_provider_park_stranded_edits(
    state: MonitorState | None,
    preserved_head: str | None,
    *,
    owned_paths: Collection[str] | None = None,
) -> None:
    """Record that a park left uncommitted repair edits for the resumed hint cycle.

    Evidence from an earlier park is dropped: the run loop's park sink records
    this park's (``record_provider_park_stranded_edits_evidence``). ``owned_paths``
    is the ownership boundary the resume commit must stay inside; an earlier
    park's boundary never carries over.
    """
    if state is not None:
        threads_addressed = state.threads_addressed_ids
        threads_addressed[PROVIDER_PARK_STRANDED_EDITS_STATE_KEY] = preserved_head or ""
        threads_addressed.pop(PROVIDER_PARK_STRANDED_EDITS_EVIDENCE_STATE_KEY, None)
        if owned_paths is None:
            threads_addressed.pop(PROVIDER_PARK_STRANDED_EDITS_OWNED_STATE_KEY, None)
        else:
            threads_addressed[PROVIDER_PARK_STRANDED_EDITS_OWNED_STATE_KEY] = json.dumps(
                sorted(owned_paths)
            )


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
    hashing cannot read records nothing, and the resume then commits none of the
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
    memory, before the resumed repair's agent can dirty the worktree again. The
    sink commits only the paths the park recorded, and only while every one still
    holds the content the park left and nothing else is dirty. Otherwise nothing
    is committed or discarded: the resume returns
    ``PARK_RESUME_UNEXPECTED_WORKTREE_CHANGES`` naming the extra and changed paths
    and keeps the marker; when the extra paths appear only after the sink has
    committed the recorded ones, the result also names those committed paths.
    Recorded edits that still will not commit return
    ``REPAIR_DIRTY_COMMIT_FAILED`` with the stranded paths, and a ``git status``
    failure is returned as-is, exactly as the repair-start guard returns it. Sink
    exceptions propagate: the caller handles them as it handles the same sink
    raising inside the repair agent's run.
    """
    preserved_head = state.threads_addressed_ids.get(PROVIDER_PARK_STRANDED_EDITS_STATE_KEY)
    if preserved_head is None:
        return None
    # The writer lock spans the evidence check and the commit, so no other AWF
    # writer can change the worktree between the two. The work slot is taken
    # first: the sink's protected-scope repair can run an agent, and one queued for
    # a slot must not hold the lock GC and recovery git ops wait on.
    async with (
        hold_work_slot(getattr(runner, "_work_slot", None), "agent"),
        hold_exclusive_worktree_writer_lock(worktree_path),
    ):
        dirty = await runner._pre_existing_dirty_repair_worktree_result(
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            operation_type=operation_type,
        )
        if dirty is not None and dirty.reason_code == _PRE_EXISTING_DIRTY_WORKTREE_REASON:
            recorded = _recorded_identities(state)
            owned = _owned_boundary(state)
            extra_paths, changed_paths = await _unexpected_worktree_changes(
                recorded, worktree_path=worktree_path, dirty_paths=_dirty_paths(dirty)
            )
            unowned_paths = sorted(recorded.keys() - owned) if owned is not None else []
            if extra_paths or changed_paths or unowned_paths:
                return _unexpected_worktree_changes_result(
                    dirty,
                    workspace_id=workspace_id,
                    preserved_head=preserved_head,
                    extra_paths=extra_paths,
                    changed_paths=changed_paths,
                    committed_paths=[],
                    unowned_paths=unowned_paths,
                )
            recorded_dirty = recorded.keys() & set(_dirty_paths(dirty))
            committed = await runner._commit_dirty_worktree(
                workspace_id=workspace_id,
                message=_RESUME_COMMIT_MESSAGE,
                compose_project=compose_project,
                compose_file=compose_file,
                state=state,
                protected_scope_revert_remote_branch=remote_branch,
                remote_push_url=remote_push_url,
                operation_start_head=preserved_head or None,
                only_paths=tuple(sorted(recorded)),
            )
            if owned is not None:
                unowned_result = await _unowned_committed_delta_result(
                    runner,
                    workspace_id=workspace_id,
                    worktree_path=worktree_path,
                    preserved_head=preserved_head,
                    owned=owned,
                )
                if unowned_result is not None:
                    return unowned_result
            dirty = await runner._pre_existing_dirty_repair_worktree_result(
                workspace_id=workspace_id,
                worktree_path=worktree_path,
                operation_type=operation_type,
            )
            # The lock only fences AWF's own writers: dirt a human added meanwhile
            # was never staged, and stays theirs to settle.
            dirty_paths = set(_dirty_paths(dirty))
            extra_paths = sorted(dirty_paths - recorded.keys())
            if dirty is not None and extra_paths:
                return _unexpected_worktree_changes_result(
                    dirty,
                    workspace_id=workspace_id,
                    preserved_head=preserved_head,
                    extra_paths=extra_paths,
                    changed_paths=[],
                    # HEAD already moved past the park's: say so, never deny it.
                    committed_paths=sorted(recorded_dirty - dirty_paths) if committed else [],
                )
    if dirty is None:
        # Row first: a write fault then leaves the in-memory marker for the next
        # in-process resume to retry, rather than a row-only one a restart reloads.
        await _drop_marker_from_workspace_row(runner, workspace_id=workspace_id)
        state.threads_addressed_ids.pop(PROVIDER_PARK_STRANDED_EDITS_STATE_KEY, None)
        state.threads_addressed_ids.pop(PROVIDER_PARK_STRANDED_EDITS_EVIDENCE_STATE_KEY, None)
        state.threads_addressed_ids.pop(PROVIDER_PARK_STRANDED_EDITS_OWNED_STATE_KEY, None)
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
    content identity here, so it never matches and is always left for a human. A
    file swapped after its type check is never followed or waited on: a symlink
    fails the open and a FIFO or other special file has no identity.
    """
    try:
        info = path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return "absent"
    if stat.S_ISLNK(info.st_mode):
        return "symlink:" + hashlib.sha256(os.fsencode(path.readlink())).hexdigest()
    if not stat.S_ISREG(info.st_mode):
        return None
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as handle:
        opened = os.fstat(handle.fileno())
        if not stat.S_ISREG(opened.st_mode):
            return None
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    return f"file:{'x' if opened.st_mode & 0o111 else '-'}:{digest}"


def _recorded_identities(state: MonitorState) -> dict[str, str | None]:
    """What the park recorded it left dirty; empty when it recorded nothing usable."""
    try:
        evidence = json.loads(
            state.threads_addressed_ids.get(PROVIDER_PARK_STRANDED_EDITS_EVIDENCE_STATE_KEY) or ""
        )
    except ValueError:
        return {}
    return cast("dict[str, str | None]", evidence) if isinstance(evidence, dict) else {}


def _owned_boundary(state: MonitorState) -> set[str] | None:
    """The ownership boundary the park recorded; ``None`` when it recorded none.

    A boundary the resume cannot read owns nothing, so no recorded path commits.
    """
    raw = state.threads_addressed_ids.get(PROVIDER_PARK_STRANDED_EDITS_OWNED_STATE_KEY)
    if raw is None:
        return None
    try:
        owned = json.loads(raw)
    except ValueError:
        return set()
    return {str(path) for path in owned} if isinstance(owned, list) else set()


async def _unowned_committed_delta_result(
    runner: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    preserved_head: str,
    owned: set[str],
) -> _GitPushResult | None:
    """Fail closed when the resume commit holds paths outside the ownership boundary.

    The pre-push dirty finalize's committed-delta gate, applied to everything
    committed on top of the park's HEAD (the sink's protected-scope repair can
    self-commit); ``None`` when the whole delta is owned.
    """
    from awf.runtime.pr_monitor_runner.pre_push_validation_dirty_finalize import (
        _committed_delta_paths,
    )
    from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult

    delta = (
        await _committed_delta_paths(
            runner, worktree_path=worktree_path, operation_start_head=preserved_head
        )
        if preserved_head
        else None
    )
    if delta is None:
        reason_code = _PRE_PUSH_DIRTY_FINALIZE_DELTA_UNAVAILABLE_REASON
        stderr = (
            "AWF committed the edits a provider park stranded but could not verify the "
            "committed delta against the operation-owned paths; nothing is pushed."
        )
        unowned: list[str] = []
    else:
        unowned = sorted(delta - owned)
        if not unowned:
            return None
        reason_code = _PRE_PUSH_DIRTY_FINALIZE_UNOWNED_DELTA_REASON
        stderr = (
            "AWF committed the edits a provider park stranded, but the commit holds "
            f"paths outside the operation-owned delta ({_path_summary(unowned)}); "
            "nothing is pushed."
        )
    _log.warning(
        "monitor.provider_park_resume_unowned_committed_delta",
        workspace_id=workspace_id,
        reason_code=reason_code,
        preserved_head=preserved_head or None,
        unowned_paths=unowned,
    )
    return _GitPushResult(
        pushed=False,
        failed=True,
        returncode=1,
        stderr=stderr,
        reason_code=reason_code,
        details={
            "phase": "provider_park_resume",
            "preserved_head": preserved_head or None,
            "paths": unowned,
            "pushed": False,
        },
    )


async def _unexpected_worktree_changes(
    recorded: dict[str, str | None], *, worktree_path: Path, dirty_paths: list[str]
) -> tuple[list[str], list[str]]:
    """Return the dirty paths the park did not record, and the recorded paths that changed.

    Every recorded path is checked, dirty or not: a stranded edit someone reverted
    while parked would otherwise let the rest of the park's edits commit without it.
    A recorded path the resume cannot read counts as changed.
    """
    extra_paths = sorted(path for path in dirty_paths if path not in recorded)
    try:
        current = await asyncio.to_thread(_path_identities, worktree_path, list(recorded))
    except OSError:
        return extra_paths, sorted(recorded)
    changed_paths = sorted(
        path for path, identity in current.items() if identity is None or identity != recorded[path]
    )
    return extra_paths, changed_paths


def _path_summary(paths: list[str]) -> str:
    shown = ", ".join(paths[:_MESSAGE_PATH_LIMIT])
    hidden = len(paths) - _MESSAGE_PATH_LIMIT
    return f"{shown} and {hidden} more" if hidden > 0 else shown


def _unexpected_worktree_changes_result(
    dirty: _GitPushResult,
    *,
    workspace_id: str,
    preserved_head: str,
    extra_paths: list[str],
    changed_paths: list[str],
    committed_paths: list[str],
    unowned_paths: list[str] | None = None,
) -> _GitPushResult:
    from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult

    details = dict(dirty.details or {})
    details.update(
        phase="provider_park_resume",
        preserved_head=preserved_head or None,
        extra_paths=extra_paths,
        changed_paths=changed_paths,
        committed_paths=committed_paths,
        unowned_paths=unowned_paths or [],
    )
    _log.warning(
        "monitor.provider_park_resume_unexpected_worktree_changes",
        workspace_id=workspace_id,
        reason_code=_PARK_RESUME_UNEXPECTED_WORKTREE_CHANGES_REASON,
        preserved_head=preserved_head or None,
        extra_paths=extra_paths,
        changed_paths=changed_paths,
        committed_paths=committed_paths,
        unowned_paths=unowned_paths or [],
    )
    findings = []
    if extra_paths:
        findings.append(f"dirty paths the park did not leave: {_path_summary(extra_paths)}")
    if changed_paths:
        findings.append(f"recorded paths changed since the park: {_path_summary(changed_paths)}")
    if unowned_paths:
        findings.append(
            f"recorded paths outside the operation-owned delta: {_path_summary(unowned_paths)}"
        )
    if committed_paths:
        disposition = (
            "AWF committed the park's own edits on the local branch "
            f"({_path_summary(committed_paths)}) but left those changes uncommitted"
        )
    else:
        disposition = "AWF committed none of them and left them"
    return _GitPushResult(
        pushed=False,
        failed=True,
        returncode=1,
        stderr=(
            "A managed provider key park left uncommitted repair edits, but the repair "
            f"worktree holds changes the park did not leave; {disposition} untouched "
            f"for a human ({'; '.join(findings)})."
        ),
        reason_code=_PARK_RESUME_UNEXPECTED_WORKTREE_CHANGES_REASON,
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
        threads_addressed.pop(PROVIDER_PARK_STRANDED_EDITS_OWNED_STATE_KEY, None)
        ws.monitor_threads_addressed = threads_addressed
        await session.commit()
