"""Read-only detection for orphaned AWF Docker resources and worktrees."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from sqlalchemy import bindparam, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.companions import parent_workspace_id_from_companion_worktree_id
from awf.common.logging import get_logger
from awf.db.enums import WorkspaceStatus
from awf.db.models import Workspace
from awf.db.session import make_engine, session_scope
from awf.service.gc import DEFAULT_MIN_AGE_HOURS
from awf.service.gc_classify import (
    PATH_ALREADY_REMOVED,
    PATH_DELETED,
)
from awf.service.gc_reconcile import ComposeTeardownOutcome, build_and_delete_gc_path

if TYPE_CHECKING:
    from awf.node.compose_manager import ComposeManager

_log = get_logger(__name__)

CHECK_TIMEOUT_SECONDS = 5.0
ORPHAN_EXAMPLE_LIMIT = 5
AWF_PROJECT_PREFIXES = ("awf_", "awf-")

# Reason codes for the flag-gated readiness-driven reaper.
ORPHAN_REAP_DISABLED = "ORPHAN_REAP_DISABLED"
ORPHAN_REAP_SKIPPED_UNKNOWN = "ORPHAN_REAP_SKIPPED_UNKNOWN"
ORPHAN_REAP_SKIPPED_YOUNG = "ORPHAN_REAP_SKIPPED_YOUNG"
ORPHAN_REAP_OK = "ORPHAN_REAP_OK"
ORPHAN_REAP_PARTIAL = "ORPHAN_REAP_PARTIAL"

# (project_name, compose_file, workspace_id) -> teardown outcome.
#
# Distinct from :data:`gc_reconcile.OrphanComposeTeardown`, which is
# ``Callable[[OrphanDirTarget], ...]``. The two are deliberately *not*
# interchangeable: gc_reconcile's reaper works from scanned ``OrphanDirTarget``
# records, whereas this resource-level reaper already holds the explicit
# ``(project_name, compose_file, workspace_id)`` triple. The differing name
# makes the incompatibility explicit rather than letting a same-named alias
# imply the closures are swappable.
OrphanResourceComposeTeardown = Callable[[str, Path, str, bool], Awaitable[ComposeTeardownOutcome]]

ResourceKind = Literal["container", "network", "volume", "worktree"]
Classification = Literal["expected", "terminal", "missing", "unknown"]

RESOURCE_KINDS: tuple[ResourceKind, ...] = ("container", "network", "volume", "worktree")
ACTIVE_WORKSPACE_STATUSES = frozenset(
    {
        WorkspaceStatus.requested.value,
        WorkspaceStatus.provisioning.value,
        WorkspaceStatus.ready.value,
        WorkspaceStatus.running.value,
        WorkspaceStatus.validating.value,
        WorkspaceStatus.pushing.value,
        WorkspaceStatus.monitoring_pr.value,
        # A blocked workspace is paused awaiting an operator decision with its
        # worktree + warm stack preserved (mirrors PROTECTED_WORKSPACE_GC_STATUSES
        # in gc_classify). It must classify as active here so the orphan reaper
        # never tears down the warm stack this state exists to keep.
        WorkspaceStatus.blocked.value,
        # A recovering workspace is paused auto-retrying across the provider
        # cooldown with its worktree + warm stack preserved (#612); it must
        # classify as active for the same reason — never reap the warm stack.
        WorkspaceStatus.recovering.value,
        WorkspaceStatus.destroying.value,
    }
)
TERMINAL_WORKSPACE_STATUSES = frozenset(
    {
        WorkspaceStatus.completed.value,
        WorkspaceStatus.failed.value,
        WorkspaceStatus.cancelled.value,
        WorkspaceStatus.destroyed.value,
    }
)
KNOWN_WORKSPACE_STATUSES = sorted(ACTIVE_WORKSPACE_STATUSES | TERMINAL_WORKSPACE_STATUSES)


class CompletedProcessLike(Protocol):
    returncode: int
    stdout: str
    stderr: str


class SubprocessRun(Protocol):
    def __call__(  # pragma: no cover - Protocol method declaration only.
        self,
        args: list[str],
        *,
        check: bool,
        capture_output: bool,
        text: Literal[True],
        timeout: float,
        env: Mapping[str, str],
    ) -> CompletedProcessLike: ...


class AsyncCommandResultLike(Protocol):
    returncode: int
    stdout: str
    stderr: str


class AsyncCommandRunnerLike(Protocol):
    async def run(  # pragma: no cover - Protocol method declaration only.
        self,
        args: list[str],
        *,
        input_bytes: bytes | None = None,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> Any: ...


@dataclass(frozen=True)
class WorkspaceIdView:
    """Snapshot of workspace ids partitioned by lifecycle."""

    active_ids: frozenset[str]
    terminal_ids: frozenset[str]
    available: bool
    retained_ids: frozenset[str] = frozenset()


@dataclass(frozen=True)
class DockerResourceCommand:
    kind: ResourceKind
    args: list[str]


@dataclass(frozen=True)
class DetectedResource:
    kind: ResourceKind
    workspace_id: str
    compose_project: str | None = None
    id: str | None = None
    name: str | None = None
    path: str | None = None
    service: str | None = None
    state: str | None = None
    status_text: str | None = None
    driver: str | None = None
    scope: str | None = None


@dataclass(frozen=True)
class ClassifiedResource:
    resource: DetectedResource
    classification: Classification
    reason: str

    @property
    def kind(self) -> ResourceKind:
        return self.resource.kind

    @property
    def workspace_id(self) -> str:
        return self.resource.workspace_id

    @property
    def compose_project(self) -> str | None:
        return self.resource.compose_project

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "kind": self.resource.kind,
            "workspace_id": self.resource.workspace_id,
            "classification": self.classification,
            "reason": self.reason,
        }
        optional: dict[str, str | None] = {
            "compose_project": self.resource.compose_project,
            "id": self.resource.id,
            "name": self.resource.name,
            "path": self.resource.path,
            "service": self.resource.service,
            "state": self.resource.state,
            "status": self.resource.status_text,
            "driver": self.resource.driver,
            "scope": self.resource.scope,
        }
        payload.update({key: value for key, value in optional.items() if value})
        return payload


@dataclass(frozen=True)
class ResourceScan:
    ok: bool
    status: str
    reason: str
    resources: tuple[DetectedResource, ...] = ()
    detail: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "ok": self.ok,
            "status": self.status,
            "reason": self.reason,
            "resource_count": len(self.resources),
        }
        if self.detail:
            payload["detail"] = self.detail
        return payload


@dataclass(frozen=True)
class CleanupReadiness:
    ready: bool
    status: str
    reason: str
    action: str
    dry_run_only: bool = True

    def to_dict(self) -> dict[str, object]:
        return {
            "ready": self.ready,
            "status": self.status,
            "reason": self.reason,
            "action": self.action,
            "dry_run_only": self.dry_run_only,
        }


@dataclass(frozen=True)
class OrphanResourceSummary:
    ok: bool
    status: str
    reason: str
    resource_count: int
    expected_count: int
    orphan_count: int
    unknown_count: int
    counts_by_kind: dict[str, int]
    orphan_counts_by_kind: dict[str, int]
    expected_counts_by_kind: dict[str, int]
    unknown_counts_by_kind: dict[str, int]
    orphan_classification_counts: dict[str, int]
    cleanup_readiness: CleanupReadiness
    scanners: dict[str, dict[str, object]]
    examples: tuple[dict[str, object], ...] = ()
    detail: str | None = None
    records: tuple[ClassifiedResource, ...] = field(default=(), repr=False)

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "ok": self.ok,
            "status": self.status,
            "reason": self.reason,
            "resource_count": self.resource_count,
            "expected_count": self.expected_count,
            "orphan_count": self.orphan_count,
            "unknown_count": self.unknown_count,
            "counts_by_kind": self.counts_by_kind,
            "orphan_counts_by_kind": self.orphan_counts_by_kind,
            "expected_counts_by_kind": self.expected_counts_by_kind,
            "unknown_counts_by_kind": self.unknown_counts_by_kind,
            "orphan_classification_counts": self.orphan_classification_counts,
            "cleanup_readiness": self.cleanup_readiness.to_dict(),
            "scanners": self.scanners,
            "examples": list(self.examples),
        }
        if self.detail:
            payload["detail"] = self.detail
        return payload


def docker_resource_commands() -> tuple[DockerResourceCommand, ...]:
    """Return the read-only Docker inventory commands used by the detector."""

    return (
        DockerResourceCommand(
            kind="container",
            args=[
                "docker",
                "ps",
                "-a",
                "--filter",
                "label=com.docker.compose.project",
                "--format",
                (
                    '{"id":{{json .ID}},"name":{{json .Names}},'
                    '"state":{{json .State}},"status":{{json .Status}},'
                    '"project":{{json (.Label "com.docker.compose.project")}},'
                    '"service":{{json (.Label "com.docker.compose.service")}}}'
                ),
            ],
        ),
        DockerResourceCommand(
            kind="network",
            args=[
                "docker",
                "network",
                "ls",
                "--filter",
                "label=com.docker.compose.project",
                "--format",
                (
                    '{"id":{{json .ID}},"name":{{json .Name}},'
                    '"project":{{json (.Label "com.docker.compose.project")}},'
                    '"driver":{{json .Driver}},"scope":{{json .Scope}}}'
                ),
            ],
        ),
        DockerResourceCommand(
            kind="volume",
            args=[
                "docker",
                "volume",
                "ls",
                "--filter",
                "label=com.docker.compose.project",
                "--format",
                (
                    '{"name":{{json .Name}},'
                    '"project":{{json (.Label "com.docker.compose.project")}},'
                    '"driver":{{json .Driver}},"scope":{{json .Scope}}}'
                ),
            ],
        ),
    )


def scan_docker_resources(
    *,
    docker_host: str,
    run_subprocess: SubprocessRun,
    timeout: float = CHECK_TIMEOUT_SECONDS,
) -> ResourceScan:
    """List AWF compose-owned Docker resources without modifying them."""

    resources: list[DetectedResource] = []
    failures: list[str] = []
    env = {**os.environ, "DOCKER_HOST": docker_host}
    for command in docker_resource_commands():
        try:
            result = run_subprocess(
                command.args,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
        except FileNotFoundError:
            return ResourceScan(
                ok=False,
                status="unavailable",
                reason="DOCKER_RESOURCE_SCAN_UNAVAILABLE",
                detail="docker binary not found on PATH",
                resources=tuple(resources),
            )
        except subprocess.TimeoutExpired as exc:
            failures.append(f"{command.kind}: {_truncate(str(exc))}")
            continue
        except Exception as exc:  # pragma: no cover - exercised through status integration.
            failures.append(f"{command.kind}: {_truncate(f'{type(exc).__name__}: {exc}')}")
            continue

        if result.returncode != 0:
            detail = _truncate(result.stderr or result.stdout) or "docker list exited non-zero"
            failures.append(f"{command.kind}: {detail}")
            continue
        resources.extend(parse_docker_resource_rows(command.kind, result.stdout))

    if failures:
        return ResourceScan(
            ok=False,
            status="unavailable",
            reason="DOCKER_RESOURCE_SCAN_UNAVAILABLE",
            detail="; ".join(failures),
            resources=tuple(resources),
        )
    return ResourceScan(
        ok=True,
        status="ok",
        reason="DOCKER_RESOURCE_SCAN_OK",
        resources=tuple(resources),
    )


async def scan_docker_resources_async(
    *,
    runner: AsyncCommandRunnerLike,
    timeout: float = CHECK_TIMEOUT_SECONDS,
) -> ResourceScan:
    """Async variant for FastAPI readiness checks."""

    resources: list[DetectedResource] = []
    failures: list[str] = []

    async def _run_command(command: DockerResourceCommand) -> AsyncCommandResultLike | Exception:
        try:
            return await asyncio.wait_for(runner.run(command.args), timeout=timeout)
        except Exception as exc:
            return exc

    commands = docker_resource_commands()
    results = await asyncio.gather(*(_run_command(command) for command in commands))
    for command, outcome in zip(commands, results, strict=True):
        if isinstance(outcome, FileNotFoundError):
            return ResourceScan(
                ok=False,
                status="unavailable",
                reason="DOCKER_RESOURCE_SCAN_UNAVAILABLE",
                detail="docker binary not found on PATH",
                resources=tuple(resources),
            )
        if isinstance(outcome, TimeoutError):
            failures.append(f"{command.kind}: docker resource scan exceeded {timeout:g}s")
            continue
        if isinstance(outcome, Exception):
            failures.append(f"{command.kind}: {_truncate(f'{type(outcome).__name__}: {outcome}')}")
            continue

        result = outcome
        if result.returncode != 0:
            detail = _truncate(result.stderr or result.stdout) or "docker list exited non-zero"
            failures.append(f"{command.kind}: {detail}")
            continue
        resources.extend(parse_docker_resource_rows(command.kind, result.stdout))

    if failures:
        return ResourceScan(
            ok=False,
            status="unavailable",
            reason="DOCKER_RESOURCE_SCAN_UNAVAILABLE",
            detail="; ".join(failures),
            resources=tuple(resources),
        )
    return ResourceScan(
        ok=True,
        status="ok",
        reason="DOCKER_RESOURCE_SCAN_OK",
        resources=tuple(resources),
    )


def scan_managed_worktrees(work_dir: str | Path) -> ResourceScan:
    """Read AWF-managed worktree directory names without deleting anything."""

    root = Path(work_dir) / "git" / "worktrees"
    if not root.exists():
        return ResourceScan(
            ok=True,
            status="ok",
            reason="WORKTREE_SCAN_OK",
            resources=(),
        )
    try:
        entries = sorted(root.iterdir(), key=lambda entry: entry.name)
    except Exception as exc:
        return ResourceScan(
            ok=False,
            status="unavailable",
            reason="WORKTREE_SCAN_UNAVAILABLE",
            detail=_truncate(f"{type(exc).__name__}: {exc}"),
            resources=(),
        )

    resources = tuple(
        DetectedResource(
            kind="worktree",
            workspace_id=parent_workspace_id_from_companion_worktree_id(entry.name) or entry.name,
            path=str(entry),
        )
        for entry in entries
        if entry.is_dir() and entry.name.startswith("ws_")
    )
    return ResourceScan(
        ok=True,
        status="ok",
        reason="WORKTREE_SCAN_OK",
        resources=resources,
    )


def parse_docker_resource_rows(kind: ResourceKind, stdout: str) -> tuple[DetectedResource, ...]:
    resources: list[DetectedResource] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        project = str(row.get("project") or "")
        workspace_id = workspace_id_from_project(project)
        if workspace_id is None:
            continue
        resources.append(
            DetectedResource(
                kind=kind,
                workspace_id=workspace_id,
                compose_project=project,
                id=_optional_str(row.get("id")),
                name=_optional_str(row.get("name")),
                service=_optional_str(row.get("service")),
                state=_optional_str(row.get("state")),
                status_text=_optional_str(row.get("status")),
                driver=_optional_str(row.get("driver")),
                scope=_optional_str(row.get("scope")),
            )
        )
    return tuple(resources)


# Shared action text for the reaping-enabled state. Both the health-check API
# (this summary) and the service-status CLI (``status.ORPHAN_REAPING_ACTION``,
# which imports this constant) must advertise identical wording for the same
# ``auto_cleanup_orphans`` flag state, so operators never see two messages.
ORPHAN_REAPING_ACTION = (
    "Reaping is enabled (auto_cleanup_orphans): the worker will tear down the "
    "listed orphaned AWF stacks and remove their worktrees automatically; no "
    "manual cleanup is required."
)


def build_orphan_resource_summary(
    *,
    docker_scan: ResourceScan,
    worktree_scan: ResourceScan,
    workspace_view: WorkspaceIdView,
    example_limit: int = ORPHAN_EXAMPLE_LIMIT,
    auto_cleanup_orphans: bool = False,
) -> OrphanResourceSummary:
    resources = (*docker_scan.resources, *worktree_scan.resources)
    records = tuple(_classify(resource, workspace_view=workspace_view) for resource in resources)

    counts_by_kind = _kind_counts(records)
    orphan_records = tuple(
        record for record in records if record.classification in {"terminal", "missing"}
    )
    expected_records = tuple(record for record in records if record.classification == "expected")
    unknown_records = tuple(record for record in records if record.classification == "unknown")
    orphan_counts_by_kind = _kind_counts(orphan_records)
    expected_counts_by_kind = _kind_counts(expected_records)
    unknown_counts_by_kind = _kind_counts(unknown_records)
    orphan_classification_counts = {
        "terminal": sum(1 for record in orphan_records if record.classification == "terminal"),
        "missing": sum(1 for record in orphan_records if record.classification == "missing"),
    }
    scanners = {
        "docker": docker_scan.to_dict(),
        "worktrees": worktree_scan.to_dict(),
    }

    # Unreliable-inventory branches run *before* the orphan-present branch.
    # A scanner that failed mid-scan (``ok=False``) can still surface partial
    # resources -- e.g. ``docker ps`` lists containers but the network/volume
    # list errored -- and those partial records can classify as orphans. If the
    # orphan-present branch ran first it would advertise reaping
    # (``dry_run_only=False``, "the worker will tear down the listed stacks")
    # for an incomplete inventory, yet ``reap_classified_orphans`` explicitly
    # skips when any scanner is unavailable. Reporting the degraded scan as
    # unknown/report-only here keeps the summary honest with the reaper so
    # health/metrics/MCP clients are never told deletion is enabled for an
    # inventory the worker will not act on.
    if not workspace_view.available:
        examples = tuple(record.to_dict() for record in unknown_records[:example_limit])
        readiness = CleanupReadiness(
            ready=False,
            status="unknown",
            reason="DB_UNAVAILABLE",
            action=("Restore control-plane database access and re-run orphan resource detection."),
        )
        return OrphanResourceSummary(
            ok=True,
            status="unknown",
            reason="DB_UNAVAILABLE",
            detail="Workspace database is unreachable; AWF resources cannot be classified.",
            resource_count=len(records),
            expected_count=len(expected_records),
            orphan_count=0,
            unknown_count=len(unknown_records),
            counts_by_kind=counts_by_kind,
            orphan_counts_by_kind=orphan_counts_by_kind,
            expected_counts_by_kind=expected_counts_by_kind,
            unknown_counts_by_kind=unknown_counts_by_kind,
            orphan_classification_counts=orphan_classification_counts,
            cleanup_readiness=readiness,
            scanners=scanners,
            examples=examples,
            records=records,
        )

    first_unavailable = _first_unavailable_scan(docker_scan, worktree_scan)
    if first_unavailable is not None:
        readiness = CleanupReadiness(
            ready=False,
            status="unknown",
            reason=first_unavailable.reason,
            action="Restore the unavailable scanner dependency and re-run orphan detection.",
        )
        return OrphanResourceSummary(
            ok=True,
            status="unavailable",
            reason=first_unavailable.reason,
            detail=first_unavailable.detail,
            resource_count=len(records),
            expected_count=len(expected_records),
            orphan_count=0,
            unknown_count=len(unknown_records),
            counts_by_kind=counts_by_kind,
            orphan_counts_by_kind=orphan_counts_by_kind,
            expected_counts_by_kind=expected_counts_by_kind,
            unknown_counts_by_kind=unknown_counts_by_kind,
            orphan_classification_counts=orphan_classification_counts,
            cleanup_readiness=readiness,
            scanners=scanners,
            records=records,
        )

    if orphan_records:
        examples = tuple(record.to_dict() for record in orphan_records[:example_limit])
        action = (
            ORPHAN_REAPING_ACTION
            if auto_cleanup_orphans
            else (
                "Review the listed AWF resources and run a non-destructive cleanup plan; "
                "this check does not remove containers, networks, volumes, or worktrees."
            )
        )
        readiness = CleanupReadiness(
            ready=False,
            status="blocked",
            reason="ORPHAN_RESOURCES_PRESENT",
            action=action,
            dry_run_only=not auto_cleanup_orphans,
        )
        return OrphanResourceSummary(
            ok=False,
            status="fail",
            reason="ORPHAN_RESOURCES_PRESENT",
            detail="Orphan AWF resources remain on this node.",
            resource_count=len(records),
            expected_count=len(expected_records),
            orphan_count=len(orphan_records),
            unknown_count=len(unknown_records),
            counts_by_kind=counts_by_kind,
            orphan_counts_by_kind=orphan_counts_by_kind,
            expected_counts_by_kind=expected_counts_by_kind,
            unknown_counts_by_kind=unknown_counts_by_kind,
            orphan_classification_counts=orphan_classification_counts,
            cleanup_readiness=readiness,
            scanners=scanners,
            examples=examples,
            records=records,
        )

    readiness = CleanupReadiness(
        ready=True,
        status="ready",
        reason="NO_ORPHANS",
        action="No orphan AWF resources were detected; no cleanup action is required.",
    )
    return OrphanResourceSummary(
        ok=True,
        status="ok",
        reason="NO_ORPHANS",
        resource_count=len(records),
        expected_count=len(expected_records),
        orphan_count=0,
        unknown_count=0,
        counts_by_kind=counts_by_kind,
        orphan_counts_by_kind=orphan_counts_by_kind,
        expected_counts_by_kind=expected_counts_by_kind,
        unknown_counts_by_kind=unknown_counts_by_kind,
        orphan_classification_counts=orphan_classification_counts,
        cleanup_readiness=readiness,
        scanners=scanners,
        records=records,
    )


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
        project_name: str, compose_file: Path, workspace_id: str, remove_volumes: bool
    ) -> ComposeTeardownOutcome:
        return await manager.teardown_project(
            project_name=project_name,
            compose_file=compose_file,
            workspace_id=workspace_id,
            remove_volumes=remove_volumes,
        )

    return _teardown


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
    if record.kind == "worktree":
        path_text = record.resource.path
        if not path_text:  # pragma: no cover - worktree records always carry a path.
            return False
        anchor = Path(path_text)
    else:
        # Docker resources (container/network/volume) anchor on the per-workspace
        # compose dir -- the same root gc_reconcile protects. If no compose dir
        # exists, there is no in-flight provision to protect (row and dir both
        # gone, only docker lingers), so the lingering stack is a genuine orphan.
        anchor = resolved_work_dir / "compose" / record.workspace_id
        if not anchor.exists():
            return True
    try:
        mtime = anchor.stat().st_mtime
    except OSError:  # pragma: no cover - reaper runs as root over its own dirs.
        return False
    return (now - mtime) >= grace_seconds


async def reap_classified_orphans(
    summary: OrphanResourceSummary,
    *,
    work_dir: Path | str,
    compose_teardown: OrphanResourceComposeTeardown,
    enabled: bool,
    now: float | None = None,
    min_age_hours: float = DEFAULT_MIN_AGE_HOURS,
) -> OrphanReapResult:
    """Reap ``terminal`` / aged ``missing`` classified orphans when ``enabled``.

    Off by default: with ``enabled=False`` this is a pure no-op (report only),
    preserving the historical ``dry_run_only`` behavior. With ``enabled=True``
    it tears down each orphaned compose stack (containers/networks/volumes) via
    WS-B1's :meth:`ComposeManager.teardown_project` and removes orphaned worktree
    directories via WS-B1's :func:`build_and_delete_gc_path` (which keeps the
    recursive byte-estimate scan off the event loop). Records classified
    ``expected`` / ``unknown`` are left untouched, and a permission refusal
    surfaces loudly (``PATH_DELETE_PERMISSION_DENIED``) as a ``partial`` run --
    never a silent success. When classification is unreliable (DB/scanner
    unavailable) it skips entirely rather than reaping on guesswork.

    ``missing`` (row-less) records carry an extra minimum-age guard
    (``min_age_hours``, mirroring :mod:`gc_reconcile`): a row-less resource whose
    on-disk artifact is younger than the grace window is left alone, since a
    just-created worktree can be visible before its workspace row commits and
    would otherwise be reaped mid-provision. ``terminal`` records are not gated.
    """
    if not enabled:
        return OrphanReapResult(
            enabled=False,
            status="disabled",
            reason_code=ORPHAN_REAP_DISABLED,
        )
    # Classification is only reliable when every scanner produced a complete
    # inventory. A scanner that failed mid-scan (``ok=False``) can still surface
    # partial resources -- e.g. ``docker ps`` lists containers but the network or
    # volume list errored -- and those partial records can classify as orphans,
    # sending ``build_orphan_resource_summary`` down the orphan-present
    # (``blocked``) branch before it ever reaches the scanner-unavailable
    # (``unknown``) branch. Reaping on that incomplete inventory would tear down
    # stacks on guesswork, so check scanner availability alongside the readiness
    # status rather than relying on ``status == "unknown"`` alone.
    scanner_unavailable = any(scanner.get("ok") is False for scanner in summary.scanners.values())
    if summary.cleanup_readiness.status == "unknown" or scanner_unavailable:
        _log.warning(
            "orphan_resources.reap_skipped_unknown",
            reason_code=ORPHAN_REAP_SKIPPED_UNKNOWN,
            summary_reason=summary.reason,
            scanner_unavailable=scanner_unavailable,
        )
        return OrphanReapResult(
            enabled=True,
            status="skipped",
            reason_code=ORPHAN_REAP_SKIPPED_UNKNOWN,
        )

    resolved_work_dir = Path(work_dir).expanduser().resolve()
    resolved_now = time.time() if now is None else now
    grace_seconds = max(0.0, min_age_hours) * 3600.0
    orphan_records: list[ClassifiedResource] = []
    for record in summary.records:
        if record.classification not in {"terminal", "missing"}:
            continue
        if record.classification == "missing" and not _missing_record_is_aged(
            record,
            resolved_work_dir=resolved_work_dir,
            grace_seconds=grace_seconds,
            now=resolved_now,
        ):
            _log.info(
                "orphan_resources.reap_skipped_young",
                reason_code=ORPHAN_REAP_SKIPPED_YOUNG,
                **record.to_dict(),
            )
            continue
        orphan_records.append(record)
    # One teardown per (project, workspace) so multiple docker resources from the
    # same stack do not trigger redundant downs.
    compose_projects: dict[tuple[str, str], None] = {}
    worktree_records: list[ClassifiedResource] = []
    # A workspace's volumes are only torn down (``docker compose down --volumes``)
    # when a volume record for it is itself cleanup-ready. A terminal workspace
    # still inside its retention window keeps volumes classified ``expected``
    # (salvage evidence) while only its live containers/networks are reaped, so
    # removing volumes here would delete the very evidence _classify protected --
    # matching the worker terminal-runtime release path's ``remove_volumes=False``.
    volume_ready_workspace_ids: set[str] = set()
    for record in orphan_records:
        if record.kind == "worktree":
            worktree_records.append(record)
            continue
        if record.kind == "volume":
            volume_ready_workspace_ids.add(record.workspace_id)
        project = record.compose_project or f"awf_{record.workspace_id}"
        compose_projects[(project, record.workspace_id)] = None

    reaped: list[OrphanReapOutcome] = []
    errors: list[OrphanReapOutcome] = []

    for project_name, workspace_id in sorted(compose_projects):
        compose_file = resolved_work_dir / "compose" / workspace_id / "compose.yml"
        remove_volumes = workspace_id in volume_ready_workspace_ids
        teardown = await compose_teardown(project_name, compose_file, workspace_id, remove_volumes)
        outcome = OrphanReapOutcome(
            kind="compose",
            workspace_id=workspace_id,
            status="reaped" if teardown.ok else "failed",
            reason_code=teardown.reason_code,
            error=teardown.error,
        )
        if teardown.ok:
            reaped.append(outcome)
            _log.info("orphan_resources.reaped_compose", **outcome.to_dict())
        else:
            errors.append(outcome)
            _log.error("orphan_resources.reap_compose_failed", **outcome.to_dict())

    for record in worktree_records:
        path_text = record.resource.path
        if not path_text:  # pragma: no cover - worktree records always carry a path.
            continue
        # Build the GC path (a recursive ``rglob`` byte estimate over a full
        # worktree checkout) and delete it inside one ``to_thread`` so the scan
        # never blocks the worker event loop -- matching WS-B1's reconciler.
        deleted, error, reason_code = await asyncio.to_thread(
            build_and_delete_gc_path, "worktree", Path(path_text), work_dir=resolved_work_dir
        )
        if deleted:
            outcome = OrphanReapOutcome(
                kind="worktree",
                workspace_id=record.workspace_id,
                status="reaped",
                reason_code=PATH_DELETED,
            )
            reaped.append(outcome)
            _log.info("orphan_resources.reaped_worktree", **outcome.to_dict())
        elif reason_code is None or reason_code == PATH_ALREADY_REMOVED:
            outcome = OrphanReapOutcome(
                kind="worktree",
                workspace_id=record.workspace_id,
                status="already_removed",
                reason_code=PATH_ALREADY_REMOVED,
            )
            reaped.append(outcome)
        else:
            outcome = OrphanReapOutcome(
                kind="worktree",
                workspace_id=record.workspace_id,
                status="failed",
                reason_code=reason_code,
                error=error,
            )
            errors.append(outcome)
            _log.error("orphan_resources.reap_worktree_failed", **outcome.to_dict())

    status: Literal["ok", "partial"] = "partial" if errors else "ok"
    return OrphanReapResult(
        enabled=True,
        status=status,
        reason_code=ORPHAN_REAP_PARTIAL if errors else ORPHAN_REAP_OK,
        reaped=tuple(reaped),
        errors=tuple(errors),
    )


async def sweep_classified_orphans(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    work_dir: Path | str,
    docker_host: str,
    compose_teardown: OrphanResourceComposeTeardown,
    enabled: bool,
    min_age_hours: float = DEFAULT_MIN_AGE_HOURS,
    min_retention_hours: float = DEFAULT_MIN_AGE_HOURS,
    run_subprocess: SubprocessRun | None = None,
) -> OrphanReapResult:
    """Scan, classify, and reap readiness-classified orphan resources.

    ``min_retention_hours`` protects retained terminal salvage during
    classification, while ``min_age_hours`` only guards row-less missing
    resources during reaping.
    """
    if not enabled:
        return OrphanReapResult(
            enabled=False,
            status="disabled",
            reason_code=ORPHAN_REAP_DISABLED,
        )

    resolved_run_subprocess = subprocess.run if run_subprocess is None else run_subprocess
    docker_scan, worktree_scan = await asyncio.gather(
        asyncio.to_thread(
            scan_docker_resources,
            docker_host=docker_host,
            run_subprocess=resolved_run_subprocess,
        ),
        asyncio.to_thread(scan_managed_worktrees, work_dir),
    )

    try:
        async with session_scope(session_factory) as session:
            workspace_view = await workspace_id_view_from_session(
                session,
                min_retention_hours=min_retention_hours,
            )
    except SQLAlchemyError as exc:
        _log.warning(
            "orphan_resources.workspace_view_unavailable",
            reason_code="DB_UNAVAILABLE",
            error_type=type(exc).__name__,
            error=str(exc)[:240],
        )
        workspace_view = unavailable_workspace_view()

    summary = build_orphan_resource_summary(
        docker_scan=docker_scan,
        worktree_scan=worktree_scan,
        workspace_view=workspace_view,
        auto_cleanup_orphans=enabled,
    )
    return await reap_classified_orphans(
        summary,
        work_dir=work_dir,
        compose_teardown=compose_teardown,
        enabled=enabled,
        min_age_hours=min_age_hours,
    )


def legacy_orphan_workspaces_payload(summary: OrphanResourceSummary) -> dict[str, object]:
    """Compatibility payload for the historical container-only check."""

    if summary.status == "unknown" and summary.reason == "DB_UNAVAILABLE":
        examples = _legacy_examples(
            [record for record in summary.records if record.classification == "unknown"]
        )
        return {
            "ok": True,
            "status": "unknown",
            "reason": "DB_UNAVAILABLE",
            "detail": "Workspace database is unreachable; cannot classify AWF resources.",
            "container_count": len(examples),
            "examples": examples[:ORPHAN_EXAMPLE_LIMIT],
            "action": "Re-run `awf service status` once the control-plane database is reachable.",
        }

    if summary.status == "unavailable":
        detail = str(summary.detail or "")
        reason = (
            "DOCKER_CLI_NOT_FOUND" if "docker binary not found" in detail else "DOCKER_UNAVAILABLE"
        )
        return {
            "ok": True,
            "status": "unavailable",
            "reason": reason,
            "detail": detail,
            "orphan_count": 0,
            "examples": [],
        }

    orphan_records = [
        record for record in summary.records if record.classification in {"terminal", "missing"}
    ]
    expected_workspace_ids = {
        record.workspace_id for record in summary.records if record.classification == "expected"
    }
    if not orphan_records:
        return {
            "ok": True,
            "status": "ok",
            "reason": "NO_ORPHANS",
            "active_count": len(expected_workspace_ids),
            "orphan_count": 0,
            "examples": [],
        }

    examples = _legacy_examples(orphan_records)
    missing_count = sum(1 for example in examples if example["classification"] == "missing")
    terminal_count = len(examples) - missing_count
    return {
        "ok": False,
        "status": "fail",
        "reason": "ORPHANS_PRESENT",
        "active_count": len(expected_workspace_ids),
        "orphan_count": len(examples),
        "orphan_missing_count": missing_count,
        "orphan_terminal_count": terminal_count,
        "examples": examples[:ORPHAN_EXAMPLE_LIMIT],
        "action": (
            "Run non-destructive orphan resource cleanup planning before removing "
            "AWF containers, networks, volumes, or worktrees."
        ),
    }


def empty_docker_scan() -> ResourceScan:
    return ResourceScan(
        ok=True,
        status="ok",
        reason="DOCKER_RESOURCE_SCAN_OK",
        resources=(),
    )


def empty_worktree_scan() -> ResourceScan:
    return ResourceScan(
        ok=True,
        status="ok",
        reason="WORKTREE_SCAN_OK",
        resources=(),
    )


def unavailable_workspace_view() -> WorkspaceIdView:
    return WorkspaceIdView(
        active_ids=frozenset(),
        terminal_ids=frozenset(),
        available=False,
        retained_ids=frozenset(),
    )


def workspace_id_from_project(project: str) -> str | None:
    for prefix in AWF_PROJECT_PREFIXES:
        if project.startswith(prefix):
            workspace_id = project[len(prefix) :]
            if workspace_id.startswith("ws_"):
                return workspace_id
    return None


async def workspace_id_view_from_session(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    min_retention_hours: float = DEFAULT_MIN_AGE_HOURS,
) -> WorkspaceIdView:
    stmt = select(Workspace.id, Workspace.status, Workspace.updated_at).where(
        Workspace.status.in_(KNOWN_WORKSPACE_STATUSES)
    )
    rows = (await session.execute(stmt)).all()
    return _workspace_view_from_rows(rows, now=now, min_retention_hours=min_retention_hours)


async def default_workspace_id_lookup(
    database_url: str,
    *,
    now: datetime | None = None,
    min_retention_hours: float = DEFAULT_MIN_AGE_HOURS,
) -> WorkspaceIdView:
    stmt = text(
        "SELECT id, status, updated_at FROM workspaces WHERE status IN :statuses"
    ).bindparams(bindparam("statuses", expanding=True))
    engine = None
    try:
        engine = make_engine(database_url)
        async with engine.connect() as conn:
            rows = (await conn.execute(stmt, {"statuses": KNOWN_WORKSPACE_STATUSES})).all()
    except SQLAlchemyError:
        return unavailable_workspace_view()
    finally:
        if engine is not None:
            await engine.dispose()
    return _workspace_view_from_rows(rows, now=now, min_retention_hours=min_retention_hours)


def summary_not_collected() -> OrphanResourceSummary:
    readiness = CleanupReadiness(
        ready=False,
        status="unknown",
        reason="ORPHAN_RESOURCE_SCAN_NOT_COLLECTED",
        action="Collect orphan resource inventory before making cleanup decisions.",
    )
    return OrphanResourceSummary(
        ok=True,
        status="unavailable",
        reason="ORPHAN_RESOURCE_SCAN_NOT_COLLECTED",
        detail="Orphan resource inventory was not collected for this summary.",
        resource_count=0,
        expected_count=0,
        orphan_count=0,
        unknown_count=0,
        counts_by_kind=_zero_kind_counts(),
        orphan_counts_by_kind=_zero_kind_counts(),
        expected_counts_by_kind=_zero_kind_counts(),
        unknown_counts_by_kind=_zero_kind_counts(),
        orphan_classification_counts={"terminal": 0, "missing": 0},
        cleanup_readiness=readiness,
        scanners={
            "docker": ResourceScan(
                ok=True,
                status="unavailable",
                reason="ORPHAN_RESOURCE_SCAN_NOT_COLLECTED",
            ).to_dict(),
            "worktrees": ResourceScan(
                ok=True,
                status="unavailable",
                reason="ORPHAN_RESOURCE_SCAN_NOT_COLLECTED",
            ).to_dict(),
        },
    )


def _workspace_view_from_rows(
    rows: Any,
    *,
    now: datetime | None = None,
    min_retention_hours: float = DEFAULT_MIN_AGE_HOURS,
) -> WorkspaceIdView:
    active: set[str] = set()
    terminal: set[str] = set()
    retained: set[str] = set()
    current_time = _to_utc(now or datetime.now(UTC))
    for row in rows:
        workspace_id, status = row[0], row[1]
        updated_at = row[2] if len(row) > 2 else None
        workspace_id_str = str(workspace_id)
        status_str = str(status)
        if status_str in ACTIVE_WORKSPACE_STATUSES:
            active.add(workspace_id_str)
        elif status_str in TERMINAL_WORKSPACE_STATUSES:
            terminal.add(workspace_id_str)
            if _within_retention(
                updated_at,
                now=current_time,
                min_retention_hours=min_retention_hours,
            ):
                retained.add(workspace_id_str)
    return WorkspaceIdView(
        active_ids=frozenset(active),
        terminal_ids=frozenset(terminal),
        available=True,
        retained_ids=frozenset(retained),
    )


def _classify(resource: DetectedResource, *, workspace_view: WorkspaceIdView) -> ClassifiedResource:
    if not workspace_view.available:
        return ClassifiedResource(
            resource=resource,
            classification="unknown",
            reason="DB_UNAVAILABLE",
        )
    if resource.workspace_id in workspace_view.active_ids:
        return ClassifiedResource(
            resource=resource,
            classification="expected",
            reason="WORKSPACE_ACTIVE",
        )
    if resource.workspace_id in workspace_view.terminal_ids:
        # Live containers and networks for a terminal workspace are leaked
        # runtime, not salvage evidence — surface them even within retention so
        # operators see the leak and the worker sweep can release them. Volumes
        # and worktrees ARE the salvage evidence; keep them expected while
        # within retention.
        if resource.kind in {"container", "network"}:
            return ClassifiedResource(
                resource=resource,
                classification="terminal",
                reason="WORKSPACE_TERMINAL_LIVE_RUNTIME",
            )
        if resource.workspace_id in workspace_view.retained_ids:
            return ClassifiedResource(
                resource=resource,
                classification="expected",
                reason="WORKSPACE_TERMINAL_WITHIN_RETENTION",
            )
        return ClassifiedResource(
            resource=resource,
            classification="terminal",
            reason="WORKSPACE_TERMINAL",
        )
    return ClassifiedResource(
        resource=resource,
        classification="missing",
        reason="WORKSPACE_MISSING",
    )


def _legacy_examples(records: list[ClassifiedResource]) -> list[dict[str, object]]:
    grouped: dict[str, dict[str, object]] = {}
    for record in records:
        key = record.compose_project or record.workspace_id
        example = grouped.setdefault(
            key,
            {
                "compose_project": record.compose_project,
                "workspace_id": record.workspace_id,
                "classification": record.classification,
                "reason": record.reason,
                "containers": [],
            },
        )
        if record.kind == "container":
            containers = example["containers"]
            assert isinstance(containers, list)
            containers.append(record.to_dict())
    return [grouped[key] for key in sorted(grouped)]


def _first_unavailable_scan(*scans: ResourceScan) -> ResourceScan | None:
    for scan in scans:
        if not scan.ok:
            return scan
    return None


def _kind_counts(records: tuple[ClassifiedResource, ...]) -> dict[str, int]:
    counts = _zero_kind_counts()
    for record in records:
        counts[record.kind] += 1
    return counts


def _zero_kind_counts() -> dict[str, int]:
    return dict.fromkeys(RESOURCE_KINDS, 0)


def _within_retention(
    updated_at: object,
    *,
    now: datetime,
    min_retention_hours: float,
) -> bool:
    if not isinstance(updated_at, datetime):
        return False
    return _to_utc(updated_at) > now - timedelta(hours=min_retention_hours)


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def _truncate(value: str, *, limit: int = 240) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "..."
