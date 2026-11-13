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
``PRE_PUSH_DIRTY_FINALIZE_DELTA_UNAVAILABLE``. That gate diffs from the
finalize's operation start, the anchor the boundary was computed from, not the
park's HEAD: the sink's protected-scope repair can self-commit before the
baseline outage parks it, and the park's HEAD already holds that commit
(PRRT_kwDOSJAM6s63OKfe).

A park marked by a pre-push validation fix pass also records the pass's baseline
(PRRT_kwDOSJAM6s63M6rz): the park skips the pass's post-sink ancestry check, so a
fix agent that rewrote HEAD off the baseline would have its rewritten tip
published without the unpushed baseline commit. Once the resume leaves the
worktree clean it applies that same check, re-parenting a non-descendant HEAD's
tree onto the baseline (or resetting to it when the rewrite carries no net
change); a failure fails closed as ``PRE_PUSH_VALIDATION_REPARENT_FAILED``.
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
from awf.node.git_manager import git_env_without_object_lookup_overrides
from awf.runtime.monitor_state_keys import (
    PROVIDER_PARK_STRANDED_EDITS_STATE_KEY as PROVIDER_PARK_STRANDED_EDITS_STATE_KEY,
)
from awf.runtime.pr_monitor_runner.constants import (
    _PARK_RESUME_UNEXPECTED_WORKTREE_CHANGES_REASON,
    _PRE_EXISTING_DIRTY_WORKTREE_REASON,
    _REPAIR_DIRTY_COMMIT_FAILED_REASON,
)
from awf.runtime.pr_monitor_runner.git_utils import git_worktree_command
from awf.runtime.pr_monitor_runner.pre_push_validation_constants import (
    _PRE_PUSH_DIRTY_FINALIZE_DELTA_UNAVAILABLE_REASON,
    _PRE_PUSH_DIRTY_FINALIZE_UNOWNED_DELTA_REASON,
    _PRE_PUSH_VALIDATION_REPARENT_FAILED_REASON,
)
from awf.runtime.worktree_writer_lock import hold_exclusive_worktree_writer_lock

if TYPE_CHECKING:
    from awf.runtime.pr_monitor import MonitorState
    from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult


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

PROVIDER_PARK_STRANDED_EDITS_DELTA_ANCHOR_STATE_KEY: Final = (
    "__awf_provider_park_stranded_edits_delta_anchor__"
)
"""Reserved ``MonitorState.threads_addressed_ids`` key: where the owned delta starts.

The pre-push dirty finalize's operation-start HEAD, recorded with its ownership
boundary; the resume's committed-delta gate diffs from it. Absent, the gate diffs
from the park's HEAD. It lives and leaves with the stranded-edits marker.
"""

PROVIDER_PARK_STRANDED_EDITS_BASELINE_STATE_KEY: Final = (
    "__awf_provider_park_stranded_edits_baseline__"
)
"""Reserved ``MonitorState.threads_addressed_ids`` key: the HEAD the resume must keep.

The pre-push validation fix pass's baseline, recorded when that pass marks the
park; absent for parks with no baseline. It lives and leaves with the
stranded-edits marker.
"""

PROVIDER_PARK_RESUME_REWRITTEN_HEAD_STATE_KEY: Final = "__awf_provider_park_resume_rewritten_head__"
"""Reserved ``MonitorState.threads_addressed_ids`` key: the parked HEAD the resume rewrote.

A JSON object ``{"from": <parked HEAD>, "to": <HEAD that replaced it>}``, recorded
when the resume re-parents a non-descendant HEAD onto the fix-pass baseline or
resets a no-net-change rewrite to it. The parked SHA is then gone by design, so a
baseline-park resume must require the replacement on the remote instead
(PRRT_kwDOSJAM6s63OA3D). It reaches the row with the marker drop and is cleared by
the next park.
"""

_RESUME_COMMIT_MESSAGE: Final = "fix: commit repair edits stranded by a managed provider key park"
_MESSAGE_PATH_LIMIT: Final = 10

_log = get_logger(__name__)


def mark_provider_park_stranded_edits(
    state: MonitorState | None,
    preserved_head: str | None,
    *,
    owned_paths: Collection[str] | None = None,
    delta_anchor: str | None = None,
    baseline_head: str | None = None,
) -> None:
    """Record that a park left uncommitted repair edits for the resumed hint cycle.

    Evidence from an earlier park is dropped: the run loop's park sink records
    this park's (``record_provider_park_stranded_edits_evidence``). ``owned_paths``
    is the ownership boundary the resume commit must stay inside, ``delta_anchor``
    the commit that boundary's committed delta starts from, and ``baseline_head``
    the commit the resumed HEAD must descend from; an earlier park's boundary,
    anchor, or baseline never carries over.
    """
    if state is not None:
        threads_addressed = state.threads_addressed_ids
        threads_addressed[PROVIDER_PARK_STRANDED_EDITS_STATE_KEY] = preserved_head or ""
        threads_addressed.pop(PROVIDER_PARK_RESUME_REWRITTEN_HEAD_STATE_KEY, None)
        threads_addressed.pop(PROVIDER_PARK_STRANDED_EDITS_EVIDENCE_STATE_KEY, None)
        if owned_paths is None:
            threads_addressed.pop(PROVIDER_PARK_STRANDED_EDITS_OWNED_STATE_KEY, None)
        else:
            threads_addressed[PROVIDER_PARK_STRANDED_EDITS_OWNED_STATE_KEY] = json.dumps(
                sorted(owned_paths)
            )
        if delta_anchor is None:
            threads_addressed.pop(PROVIDER_PARK_STRANDED_EDITS_DELTA_ANCHOR_STATE_KEY, None)
        else:
            threads_addressed[PROVIDER_PARK_STRANDED_EDITS_DELTA_ANCHOR_STATE_KEY] = delta_anchor
        if baseline_head is None:
            threads_addressed.pop(PROVIDER_PARK_STRANDED_EDITS_BASELINE_STATE_KEY, None)
        else:
            threads_addressed[PROVIDER_PARK_STRANDED_EDITS_BASELINE_STATE_KEY] = baseline_head


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


async def refresh_provider_park_stranded_edits_evidence(
    runner: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    state: MonitorState,
) -> None:
    """Re-record a still-marked park's evidence and persist it before it re-parks.

    The resumed sink's protected-scope repair can rewrite a recorded path before its
    baseline outage re-parks the workspace, so the earlier park's fingerprints would
    make the next resume reject the repair's own content as
    ``PARK_RESUME_UNEXPECTED_WORKTREE_CHANGES`` (PRRT_kwDOSJAM6s63Quqs). That repair
    can also self-commit, and the re-park's event records the new HEAD, so the
    marker's HEAD moves with it: a fix-pass re-parent keyed to the old HEAD would
    never name the replacement the next resume requires (PRRT_kwDOSJAM6s63SI0A).
    A refresh that cannot read the worktree keeps the earlier evidence rather than
    leaving the marker with none (PRRT_kwDOSJAM6s63SJ3b): the resume still checks
    every recorded path's content, so a stale fingerprint only ever fails closed.
    The ownership boundary, delta anchor and fix-pass baseline still describe the
    original operation; a boundary with no recorded anchor keeps diffing from the
    original HEAD.
    """
    threads_addressed = state.threads_addressed_ids
    parked_head = threads_addressed.get(PROVIDER_PARK_STRANDED_EDITS_STATE_KEY)
    if parked_head is None:
        return
    head = await runner._rev_parse_head(worktree_path)
    if head:
        if PROVIDER_PARK_STRANDED_EDITS_OWNED_STATE_KEY in threads_addressed:
            threads_addressed.setdefault(
                PROVIDER_PARK_STRANDED_EDITS_DELTA_ANCHOR_STATE_KEY, parked_head
            )
        threads_addressed[PROVIDER_PARK_STRANDED_EDITS_STATE_KEY] = head
    earlier_evidence = threads_addressed.pop(PROVIDER_PARK_STRANDED_EDITS_EVIDENCE_STATE_KEY, None)
    await record_provider_park_stranded_edits_evidence(
        runner, workspace_id=workspace_id, worktree_path=worktree_path, state=state
    )
    if earlier_evidence is not None:
        threads_addressed.setdefault(
            PROVIDER_PARK_STRANDED_EDITS_EVIDENCE_STATE_KEY, earlier_evidence
        )
    await runner._persist_state(workspace_id, state)


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
        owned = _owned_boundary(state)
        delta_anchor = state.threads_addressed_ids.get(
            PROVIDER_PARK_STRANDED_EDITS_DELTA_ANCHOR_STATE_KEY, preserved_head
        )
        delta_gated = False
        if dirty is not None and dirty.reason_code == _PRE_EXISTING_DIRTY_WORKTREE_REASON:
            recorded = _recorded_identities(state)
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
                    delta_anchor=delta_anchor,
                    owned=owned,
                )
                if unowned_result is not None:
                    return unowned_result
                delta_gated = True
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
            # A clean worktree may hold an earlier resume's rejected commit, so it
            # passes the same committed-delta gate before the marker can go.
            if owned is not None and not delta_gated:
                unowned_result = await _unowned_committed_delta_result(
                    runner,
                    workspace_id=workspace_id,
                    worktree_path=worktree_path,
                    preserved_head=preserved_head,
                    delta_anchor=delta_anchor,
                    owned=owned,
                )
                if unowned_result is not None:
                    return unowned_result
            # The worktree is clean, so re-parenting cannot discard anyone's edits.
            baseline_result = await _keep_fix_pass_baseline(
                runner,
                workspace_id=workspace_id,
                worktree_path=worktree_path,
                state=state,
                parked_head=preserved_head,
            )
            if baseline_result is not None:
                return baseline_result
    if dirty is None:
        # Row first: a write fault then leaves the in-memory marker for the next
        # in-process resume to retry, rather than a row-only one a restart reloads.
        await _drop_marker_from_workspace_row(
            runner,
            workspace_id=workspace_id,
            rewritten_head=state.threads_addressed_ids.get(
                PROVIDER_PARK_RESUME_REWRITTEN_HEAD_STATE_KEY
            ),
        )
        state.threads_addressed_ids.pop(PROVIDER_PARK_STRANDED_EDITS_STATE_KEY, None)
        state.threads_addressed_ids.pop(PROVIDER_PARK_STRANDED_EDITS_EVIDENCE_STATE_KEY, None)
        state.threads_addressed_ids.pop(PROVIDER_PARK_STRANDED_EDITS_OWNED_STATE_KEY, None)
        state.threads_addressed_ids.pop(PROVIDER_PARK_STRANDED_EDITS_DELTA_ANCHOR_STATE_KEY, None)
        state.threads_addressed_ids.pop(PROVIDER_PARK_STRANDED_EDITS_BASELINE_STATE_KEY, None)
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
    delta_anchor: str,
    owned: set[str],
) -> _GitPushResult | None:
    """Fail closed when the resume commit holds paths outside the ownership boundary.

    The pre-push dirty finalize's committed-delta gate, applied to everything
    committed since ``delta_anchor`` — the finalize's operation start, so a
    protected-scope repair self-commit from the parked sink or the resumed one is
    inspected too; ``None`` when the whole delta is owned.
    """
    from awf.runtime.pr_monitor_runner.pre_push_validation_dirty_finalize import (
        _committed_delta_paths,
    )
    from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult

    delta = (
        await _committed_delta_paths(
            runner, worktree_path=worktree_path, operation_start_head=delta_anchor
        )
        if delta_anchor
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
        operation_start_head=delta_anchor or None,
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
            "operation_start_head": delta_anchor or None,
            "paths": unowned,
            "pushed": False,
        },
    )


def provider_park_resume_kept_head(state: MonitorState, parked_head: str | None) -> str | None:
    """Return the commit that now carries ``parked_head``: the resume's rewrite, if any.

    Only a record whose ``from`` is exactly ``parked_head`` applies, so a stale or
    unreadable record never stands in for a different park's kept commit.
    """
    raw = state.threads_addressed_ids.get(PROVIDER_PARK_RESUME_REWRITTEN_HEAD_STATE_KEY)
    if not parked_head or not raw:
        return parked_head
    try:
        record = json.loads(raw)
    except ValueError:
        return parked_head
    if not isinstance(record, dict) or record.get("from") != parked_head:
        return parked_head
    replacement = record.get("to")
    return replacement if isinstance(replacement, str) and replacement else parked_head


async def _keep_fix_pass_baseline(
    runner: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    state: MonitorState,
    parked_head: str,
) -> _GitPushResult | None:
    """Keep the resumed HEAD on the fix pass's baseline; ``None`` once it descends.

    The fix pass's post-sink ancestry check, applied to the clean resumed worktree:
    a non-descendant HEAD's tree is re-parented onto the baseline, and a rewrite
    with no net change is reset to the baseline, so the push never drops it. The
    replacement is keyed to ``parked_head``, not the resumed HEAD: the resume's own
    stranded-edits commit sits between them (PRRT_kwDOSJAM6s63OyKh).
    """
    baseline = state.threads_addressed_ids.get(PROVIDER_PARK_STRANDED_EDITS_BASELINE_STATE_KEY)
    if not baseline:
        return None
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass import (
        _head_descends_from,
        _reparent_fix_pass_commit,
    )
    from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult

    head = await runner._rev_parse_head(worktree_path)
    if head is not None:
        if head == baseline or await _head_descends_from(
            runner, worktree_path=worktree_path, ancestor=baseline, descendant=head
        ):
            return None
        new_head, no_net_change, failure_reason = await _reparent_fix_pass_commit(
            runner,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            fix_start_head=baseline,
            current_head=head,
            pass_number=0,
            task_tag=await runner._resolve_task_tag(workspace_id),
        )
        if no_net_change:
            reset = await runner._deps.runner.run(
                git_worktree_command(worktree_path, "reset", "--hard", baseline),
                env=git_env_without_object_lookup_overrides(),
            )
            failure_reason = None if reset.ok else _PRE_PUSH_VALIDATION_REPARENT_FAILED_REASON
        if failure_reason is None:
            state.threads_addressed_ids[PROVIDER_PARK_RESUME_REWRITTEN_HEAD_STATE_KEY] = json.dumps(
                {"from": parked_head or head, "to": new_head or baseline}
            )
            _log.info(
                "monitor.provider_park_resume_reparented",
                workspace_id=workspace_id,
                from_sha=head,
                to_sha=new_head or baseline,
                fix_start_head=baseline,
            )
            return None
    _log.warning(
        "monitor.provider_park_resume_reparent_failed",
        workspace_id=workspace_id,
        reason_code=_PRE_PUSH_VALIDATION_REPARENT_FAILED_REASON,
        head=head,
        fix_start_head=baseline,
    )
    return _GitPushResult(
        pushed=False,
        failed=True,
        returncode=1,
        stderr=(
            "AWF committed the edits a provider park stranded, but could not keep the "
            "resumed HEAD on the validation fix pass's baseline commit; nothing is pushed."
        ),
        reason_code=_PRE_PUSH_VALIDATION_REPARENT_FAILED_REASON,
        details={
            "phase": "provider_park_resume",
            "head": head,
            "fix_start_head": baseline,
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


async def _drop_marker_from_workspace_row(
    runner: Any, *, workspace_id: str, rewritten_head: str | None = None
) -> None:
    """Remove ONLY the stranded-edits marker (and its evidence) from the persisted row.

    A ``rewritten_head`` record is written in the same commit, so a restart that
    reloads the row without the marker still knows which commit replaced the parked
    HEAD.

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
        threads_addressed.pop(PROVIDER_PARK_STRANDED_EDITS_DELTA_ANCHOR_STATE_KEY, None)
        threads_addressed.pop(PROVIDER_PARK_STRANDED_EDITS_BASELINE_STATE_KEY, None)
        if rewritten_head is not None:
            threads_addressed[PROVIDER_PARK_RESUME_REWRITTEN_HEAD_STATE_KEY] = rewritten_head
        ws.monitor_threads_addressed = threads_addressed
        await session.commit()
