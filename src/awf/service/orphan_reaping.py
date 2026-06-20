"""Reap-result data types and pure helpers for orphan-resource reaping.

Split out of :mod:`awf.service.orphan_resources` to keep that module under the
first-party file-size guardrail. This holds the reap-outcome dataclasses, the
compose-teardown closure builder, and the row-less orphan aging/``--limit``
helpers -- the pure, lookup-free support layer for ``reap_classified_orphans``
and ``sweep_classified_orphans``, which stay in
:mod:`awf.service.orphan_resources` because their behavior is pinned to that
module's monkeypatched scan/classify seam. Nothing here imports from
``orphan_resources`` at runtime (only annotation-only types), so the two modules
do not form an import cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from awf.node.compose_manager import ComposeManager
    from awf.service.gc_reconcile import ComposeTeardownOutcome
    from awf.service.orphan_resources import ClassifiedResource, OrphanResourceComposeTeardown


@dataclass(frozen=True)
class OrphanReapOutcome:
    """Outcome of reaping one classified orphan (a compose stack or a worktree)."""

    kind: Literal["compose", "worktree"]
    workspace_id: str
    status: Literal["reaped", "already_removed", "failed"]
    reason_code: str
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "kind": self.kind,
            "workspace_id": self.workspace_id,
            "status": self.status,
            "reason_code": self.reason_code,
        }
        if self.error is not None:
            payload["error"] = self.error
        return payload


@dataclass(frozen=True)
class OrphanReapResult:
    """Result of a flag-gated readiness-driven reap pass over a summary."""

    enabled: bool
    status: Literal["disabled", "skipped", "ok", "partial"]
    reason_code: str
    reaped: tuple[OrphanReapOutcome, ...] = ()
    errors: tuple[OrphanReapOutcome, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "status": self.status,
            "reason_code": self.reason_code,
            "reaped": [outcome.to_dict() for outcome in self.reaped],
            "errors": [outcome.to_dict() for outcome in self.errors],
        }


def build_orphan_compose_teardown(manager: ComposeManager) -> OrphanResourceComposeTeardown:
    """Compose-teardown closure over a ``ComposeManager`` (WS-B1 path).

    The caller decides ``remove_volumes`` per workspace: a terminal workspace
    that is still within its retention window has its live containers/networks
    classified ``terminal`` (reapable leaked runtime) while its volumes stay
    ``expected`` salvage evidence, so tearing the stack down must not pass
    ``--volumes`` and delete those protected volumes. This mirrors the worker
    terminal-runtime release path (:mod:`awf.control.worker.cleanup`), which
    tears down retained-terminal runtime with ``remove_volumes=False``.
    """

    async def _teardown(
        project_name: str,
        compose_file: Path,
        workspace_id: str,
        remove_volumes: bool,
        *,
        fallback_volume_names: tuple[str, ...] = (),
    ) -> ComposeTeardownOutcome:
        return await manager.teardown_project(
            project_name=project_name,
            compose_file=compose_file,
            workspace_id=workspace_id,
            remove_volumes=remove_volumes,
            fallback_volume_names=fallback_volume_names,
        )

    return _teardown


def _orphan_record_anchor(record: ClassifiedResource, *, resolved_work_dir: Path) -> Path | None:
    """On-disk artifact a row-less orphan's age is read from.

    Worktree records anchor on their checkout path; docker
    (container/network/volume) records anchor on the per-workspace compose dir --
    the same roots :func:`gc_reconcile` protects. Both the grace gate
    (:func:`_missing_record_is_aged`) and the oldest-first ``--limit`` ordering
    (:func:`_limit_records_to_oldest_workspaces`) read this single anchor so the two
    never drift. ``None`` only when a worktree record carries no path (defensive;
    managed worktree records always do).
    """
    if record.kind == "worktree":
        path_text = record.resource.path
        return Path(path_text) if path_text else None
    return resolved_work_dir / "compose" / record.workspace_id


def _orphan_record_anchor_mtime(
    record: ClassifiedResource, *, resolved_work_dir: Path
) -> float | None:
    """``mtime`` of a row-less orphan's on-disk anchor, or ``None`` when undatable."""
    anchor = _orphan_record_anchor(record, resolved_work_dir=resolved_work_dir)
    if anchor is None:  # pragma: no cover - worktree records always carry a path.
        return None
    try:
        return anchor.stat().st_mtime
    except OSError:
        return None


def _missing_record_is_aged(
    record: ClassifiedResource,
    *,
    resolved_work_dir: Path,
    grace_seconds: float,
    now: float,
) -> bool:
    """Confirm a row-less (``missing``) resource is older than the grace window.

    Mirrors :func:`gc_reconcile.scan_orphan_workspace_dirs`'s minimum-age grace:
    a just-created worktree (or compose stack) can be visible on the filesystem
    before its workspace row commits, and during that window :func:`_classify`
    returns ``WORKSPACE_MISSING``. Reaping it would delete an in-flight provision
    rather than a confirmed orphan, so a ``missing`` record is only reaped once
    its on-disk provision artifact is older than ``grace_seconds``. ``terminal``
    records skip this check entirely -- their workspace row confirms they are
    done, so they are not gated here.
    """
    if grace_seconds <= 0.0:
        return True
    anchor = _orphan_record_anchor(record, resolved_work_dir=resolved_work_dir)
    if anchor is None:  # pragma: no cover - worktree records always carry a path.
        return False
    # A docker resource whose per-workspace compose dir is gone has no in-flight
    # provision to protect (row and dir both gone, only docker lingers), so the
    # lingering stack is a genuine orphan.
    if record.kind != "worktree" and not anchor.exists():
        return True
    try:
        mtime = anchor.stat().st_mtime
    except OSError:  # pragma: no cover - reaper runs as root over its own dirs.
        return False
    return (now - mtime) >= grace_seconds


def _limit_records_to_oldest_workspaces(
    records: list[ClassifiedResource],
    *,
    limit: int,
    resolved_work_dir: Path,
) -> list[ClassifiedResource]:
    """Keep records for at most ``limit`` distinct workspaces, oldest on-disk first.

    Bounds the additive row-less orphan sweep to the operator's ``--limit`` batch so
    ``awf service gc --execute --limit N`` cannot let the sweep tear down every aged
    row-less orphan in one pass while the DB-row terminal reaper honours the same N --
    the cross-pass consistency PRRT_kwDOSJAM6s6LCCJZ asked for. Bounds DISTINCT
    workspaces, not records: a workspace surfaces several records (worktree +
    container/network/volume) and :func:`reap_classified_orphans` tears its compose
    stack down as a unit, so a record-level cap could half-reap a stack. "Oldest" is
    the oldest on-disk anchor ``mtime`` across a workspace's records -- a row-less
    orphan has no DB row (hence no ``updated_at``) to sort on, so this reuses the same
    anchor the age gate reads; an undatable anchor sorts oldest (reaped first) and ties
    break on ``workspace_id`` for determinism. The terminal pass orders by DB
    ``updated_at`` while this pass orders by disk ``mtime``, so ``--limit N`` bounds
    each pass to N rather than yielding one globally oldest-N set -- the approximation
    the absence of a shared sort key forces.
    """
    workspace_age: dict[str, float] = {}
    for record in records:
        mtime = _orphan_record_anchor_mtime(record, resolved_work_dir=resolved_work_dir)
        age_key = float("-inf") if mtime is None else mtime
        existing = workspace_age.get(record.workspace_id)
        if existing is None or age_key < existing:
            workspace_age[record.workspace_id] = age_key
    kept = {
        workspace_id
        for workspace_id, _ in sorted(workspace_age.items(), key=lambda item: (item[1], item[0]))[
            :limit
        ]
    }
    return [record for record in records if record.workspace_id in kept]
