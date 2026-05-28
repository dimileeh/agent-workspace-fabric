"""Read-only orphan resource detection for local service status."""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol

from awf.common.companions import parent_workspace_id_from_companion_worktree_id
from awf.db.enums import WorkspaceStatus
from awf.service.gc import (
    DEFAULT_MIN_AGE_HOURS,
    PROTECTED_WORKSPACE_GC_STATUSES,
    TERMINAL_WORKSPACE_GC_STATUSES,
)
from awf.service.profile_metadata import NetworkPosture

CHECK_TIMEOUT_SECONDS = 5.0
ORPHAN_EXAMPLE_LIMIT = 5

ACTIVE_WORKSPACE_STATUSES = frozenset(PROTECTED_WORKSPACE_GC_STATUSES)
TERMINAL_WORKSPACE_STATUSES = frozenset(TERMINAL_WORKSPACE_GC_STATUSES)
KNOWN_WORKSPACE_STATUSES = tuple(sorted(ACTIVE_WORKSPACE_STATUSES | TERMINAL_WORKSPACE_STATUSES))

_AWF_PROJECT_PREFIXES = ("awf_", "awf-")
_RESOURCE_KINDS = ("container", "network", "volume", "worktree")
_WORKSPACE_ID_PATTERN = r"ws_[A-Za-z0-9][A-Za-z0-9_]*"
_WORKSPACE_ID_RE = re.compile(rf"^{_WORKSPACE_ID_PATTERN}$")
_HYPHEN_DELIMITED_MANAGED_TAIL_RE = re.compile(rf"^(?P<workspace_id>{_WORKSPACE_ID_PATTERN})-")


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


@dataclass(frozen=True)
class WorkspaceLifecycleSnapshot:
    """One workspace row needed to classify managed resources."""

    workspace_id: str
    status: str
    updated_at: datetime | None
    compose_project_name: str | None = None
    compose_file_path: str | None = None
    pr_url: str | None = None
    network_posture: NetworkPosture | None = None
    allowlist_templates: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkspaceIdView:
    """Workspace ids and lifecycle snapshots used by orphan detection."""

    active_ids: frozenset[str]
    terminal_ids: frozenset[str]
    available: bool
    snapshots: tuple[WorkspaceLifecycleSnapshot, ...] = ()


@dataclass(frozen=True)
class ManagedResource:
    """One Docker or filesystem resource managed by AWF."""

    resource_kind: str
    resource_id: str
    resource_name: str
    workspace_id: str | None
    compose_project: str | None = None
    detail: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ResourceFinding:
    """Structured operator-facing resource classification."""

    resource_kind: str
    resource_id: str
    resource_name: str
    workspace_id: str | None
    reason: str
    classification: str
    suggested_action: str
    compose_project: str | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "resource_kind": self.resource_kind,
            "resource_id": self.resource_id,
            "resource_name": self.resource_name,
            "reason": self.reason,
            "classification": self.classification,
            "suggested_action": self.suggested_action,
        }
        if self.workspace_id is not None:
            payload["workspace_id"] = self.workspace_id
        if self.compose_project is not None:
            payload["compose_project"] = self.compose_project
        if self.detail:
            payload["detail"] = self.detail
        return payload


@dataclass(frozen=True)
class OrphanResourceSummary:
    """Serializable orphan resource detector result."""

    resource_counts: dict[str, int]
    expected_counts_by_kind: dict[str, int]
    retained_counts_by_kind: dict[str, int]
    orphan_counts_by_kind: dict[str, int]
    examples: tuple[ResourceFinding, ...]
    warnings: tuple[ResourceFinding, ...]
    active_workspace_ids: frozenset[str] = frozenset()
    unknown_examples: tuple[ResourceFinding, ...] = ()

    @property
    def expected_count(self) -> int:
        return sum(self.expected_counts_by_kind.values())

    @property
    def retained_count(self) -> int:
        return sum(self.retained_counts_by_kind.values())

    @property
    def orphan_count(self) -> int:
        return sum(self.orphan_counts_by_kind.values())

    @property
    def warning_count(self) -> int:
        return len(self.warnings)

    @property
    def status(self) -> str:
        if self.unknown_examples:
            return "unknown"
        if self.orphan_count:
            return "fail"
        if self.warnings:
            if any(
                warning.reason in {"DOCKER_CLI_NOT_FOUND", "DOCKER_UNAVAILABLE"}
                for warning in self.warnings
            ):
                return "unavailable"
            return "warn"
        return "ok"

    @property
    def reason(self) -> str:
        if self.unknown_examples:
            return "DB_UNAVAILABLE"
        if self.orphan_count:
            return "ORPHANS_PRESENT"
        if self.warnings:
            return self.warnings[0].reason
        return "NO_ORPHANS"

    @property
    def ok(self) -> bool:
        return self.status != "fail"

    def to_check_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "ok": self.ok,
            "status": self.status,
            "reason": self.reason,
            "resource_count": sum(self.resource_counts.values()),
            "resource_counts": _nonzero_counts(self.resource_counts),
            "active_count": len(self.active_workspace_ids),
            "expected_count": self.expected_count,
            "expected_counts_by_kind": _nonzero_counts(self.expected_counts_by_kind),
            "retained_count": self.retained_count,
            "retained_counts_by_kind": _nonzero_counts(self.retained_counts_by_kind),
            "orphan_count": self.orphan_count,
            "orphan_counts_by_kind": _nonzero_counts(self.orphan_counts_by_kind),
            "warning_count": self.warning_count,
            "warnings": [warning.to_dict() for warning in self.warnings],
            "examples": [example.to_dict() for example in self._payload_examples()],
            "container_count": self.resource_counts.get("container", 0),
            "orphan_missing_count": sum(
                1 for example in self.examples if example.reason == "WORKSPACE_MISSING"
            ),
            "orphan_terminal_count": sum(
                1
                for example in self.examples
                if example.reason == "WORKSPACE_TERMINAL_RETENTION_EXPIRED"
            ),
        }
        if self.unknown_examples:
            payload["detail"] = (
                "Workspace database is unreachable; cannot classify AWF managed resources."
            )
            payload["action"] = (
                "Re-run `awf service status` once the control-plane database is reachable."
            )
        elif self.orphan_count:
            payload["action"] = (
                "Inspect the listed resources, then use the existing explicit workspace "
                "cleanup or service GC path after confirming no active workspace owns them."
            )
        elif self.warnings:
            payload["action"] = self.warnings[0].suggested_action
            payload["detail"] = self.warnings[0].detail or ""
        return payload

    def _payload_examples(self) -> tuple[ResourceFinding, ...]:
        if self.unknown_examples:
            return self.unknown_examples[:ORPHAN_EXAMPLE_LIMIT]
        return self.examples[:ORPHAN_EXAMPLE_LIMIT]


def detect_orphan_resources(
    *,
    work_dir: Path | str,
    docker_host: str,
    workspace_view: WorkspaceIdView,
    run_subprocess: SubprocessRun,
    now: datetime | None = None,
    min_retention_hours: float = DEFAULT_MIN_AGE_HOURS,
) -> OrphanResourceSummary:
    """Detect AWF-managed resources that are cleanup-ready.

    This function never deletes resources. It only shells out to Docker list
    commands and scans the AWF-managed worktree root.
    """

    current_time = _to_utc(now or datetime.now(UTC))
    workspaces = _workspace_snapshots(workspace_view)
    project_to_workspace = _compose_project_index(workspaces)

    docker_resources, warnings = _collect_docker_resources(
        docker_host=docker_host,
        run_subprocess=run_subprocess,
        project_to_workspace=project_to_workspace,
    )
    worktree_resources, worktree_warnings = _scan_worktrees(Path(work_dir).expanduser())
    resources = docker_resources + worktree_resources
    warnings.extend(worktree_warnings)

    resource_counts = _kind_counts(resources)
    expected: Counter[str] = Counter()
    retained: Counter[str] = Counter()
    orphan_counts: Counter[str] = Counter()
    examples: list[ResourceFinding] = []
    unknown_examples: list[ResourceFinding] = []
    active_workspace_ids: set[str] = set()

    for resource in resources:
        finding = _classify_resource(
            resource,
            workspace_view=workspace_view,
            workspaces=workspaces,
            now=current_time,
            min_retention_hours=min_retention_hours,
        )
        if finding.classification == "expected":
            expected[resource.resource_kind] += 1
            active_workspace_ids.add(str(resource.workspace_id))
        elif finding.classification == "retained":
            retained[resource.resource_kind] += 1
        elif finding.classification == "unknown":
            unknown_examples.append(finding)
        else:
            orphan_counts[resource.resource_kind] += 1
            examples.append(finding)

    return OrphanResourceSummary(
        resource_counts=dict(resource_counts),
        expected_counts_by_kind=dict(expected),
        retained_counts_by_kind=dict(retained),
        orphan_counts_by_kind=dict(orphan_counts),
        examples=tuple(examples),
        warnings=tuple(warnings),
        active_workspace_ids=frozenset(active_workspace_ids),
        unknown_examples=tuple(unknown_examples),
    )


def workspace_id_from_project(
    project: str,
    *,
    project_to_workspace: Mapping[str, str] | None = None,
) -> str | None:
    """Return an AWF workspace id from a Compose project name when safe."""

    if project_to_workspace is not None and project in project_to_workspace:
        return project_to_workspace[project]
    for prefix in _AWF_PROJECT_PREFIXES:
        if project.startswith(prefix):
            workspace_id = project.removeprefix(prefix)
            if _looks_like_workspace_id(workspace_id):
                return workspace_id
    return None


def _collect_docker_resources(
    *,
    docker_host: str,
    run_subprocess: SubprocessRun,
    project_to_workspace: Mapping[str, str],
) -> tuple[list[ManagedResource], list[ResourceFinding]]:
    resources: list[ManagedResource] = []
    warnings: list[ResourceFinding] = []
    commands = _docker_list_commands()
    for index, (kind, args) in enumerate(commands):
        result = _run_docker_command(args, docker_host=docker_host, run_subprocess=run_subprocess)
        failure = _docker_failure_finding(result, resource_kind=kind)
        if failure is not None:
            warnings.append(failure)
            warnings.extend(
                _docker_skipped_findings(
                    commands[index + 1 :],
                    failed_kind=kind,
                    failure=failure,
                )
            )
            break
        assert not isinstance(result, Exception)
        parsed, parse_warnings = _parse_docker_lines(
            result.stdout,
            resource_kind=kind,
            project_to_workspace=project_to_workspace,
        )
        resources.extend(parsed)
        warnings.extend(parse_warnings)
    return resources, warnings


def _docker_skipped_findings(
    commands: tuple[tuple[str, list[str]], ...],
    *,
    failed_kind: str,
    failure: ResourceFinding,
) -> list[ResourceFinding]:
    failed_detail = f": {failure.detail}" if failure.detail else ""
    return [
        _warning(
            resource_kind=kind,
            reason="DOCKER_SCAN_SKIPPED",
            detail=f"not scanned because {failed_kind} list failed{failed_detail}",
        )
        for kind, _args in commands
    ]


def _docker_list_commands() -> tuple[tuple[str, list[str]], ...]:
    return (
        (
            "container",
            [
                "docker",
                "ps",
                "-a",
                "--filter",
                "label=com.docker.compose.project",
                "--format",
                (
                    '{"id":{{json .ID}},"name":{{json .Names}},'
                    '"state":{{json .State}},"status":{{json .Status}},'
                    '"command":{{json .Command}},'
                    '"project":{{json (.Label "com.docker.compose.project")}},'
                    '"service":{{json (.Label "com.docker.compose.service")}}}'
                ),
            ],
        ),
        (
            "network",
            [
                "docker",
                "network",
                "ls",
                "--filter",
                "label=com.docker.compose.project",
                "--format",
                (
                    '{"id":{{json .ID}},"name":{{json .Name}},'
                    '"driver":{{json .Driver}},"scope":{{json .Scope}},'
                    '"project":{{json (.Label "com.docker.compose.project")}}}'
                ),
            ],
        ),
        (
            "volume",
            [
                "docker",
                "volume",
                "ls",
                "--filter",
                "label=com.docker.compose.project",
                "--format",
                (
                    '{"name":{{json .Name}},"driver":{{json .Driver}},'
                    '"mountpoint":{{json .Mountpoint}},'
                    '"project":{{json (.Label "com.docker.compose.project")}}}'
                ),
            ],
        ),
    )


def _run_docker_command(
    args: list[str],
    *,
    docker_host: str,
    run_subprocess: SubprocessRun,
) -> CompletedProcessLike | Exception:
    env = {**os.environ, "DOCKER_HOST": docker_host}
    try:
        return run_subprocess(
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=CHECK_TIMEOUT_SECONDS,
            env=env,
        )
    except Exception as exc:
        return exc


def _docker_failure_finding(
    result: CompletedProcessLike | Exception,
    *,
    resource_kind: str,
) -> ResourceFinding | None:
    if isinstance(result, FileNotFoundError):
        return _warning(
            resource_kind=resource_kind,
            reason="DOCKER_CLI_NOT_FOUND",
            detail="docker binary not found on PATH",
        )
    if isinstance(result, subprocess.TimeoutExpired):
        return _warning(
            resource_kind=resource_kind,
            reason="DOCKER_UNAVAILABLE",
            detail=_truncate(str(result)),
        )
    if isinstance(result, Exception):
        return _warning(
            resource_kind=resource_kind,
            reason="DOCKER_UNAVAILABLE",
            detail=_truncate(f"{type(result).__name__}: {result}"),
        )
    if result.returncode != 0:
        detail = _truncate(result.stderr or result.stdout) or "docker list command exited non-zero"
        return _warning(
            resource_kind=resource_kind,
            reason="DOCKER_UNAVAILABLE",
            detail=detail,
        )
    return None


def _parse_docker_lines(
    stdout: str,
    *,
    resource_kind: str,
    project_to_workspace: Mapping[str, str],
) -> tuple[list[ManagedResource], list[ResourceFinding]]:
    resources: list[ManagedResource] = []
    warnings: list[ResourceFinding] = []
    for line_number, line in enumerate(stdout.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            warnings.append(
                _warning(
                    resource_kind=resource_kind,
                    reason="DOCKER_OUTPUT_MALFORMED",
                    detail=f"line {line_number}: {exc.msg}",
                )
            )
            continue
        if not isinstance(row, dict):
            warnings.append(
                _warning(
                    resource_kind=resource_kind,
                    reason="DOCKER_OUTPUT_MALFORMED",
                    detail=f"line {line_number}: expected object",
                )
            )
            continue
        resource = _resource_from_docker_row(
            resource_kind,
            row,
            project_to_workspace=project_to_workspace,
        )
        if resource is not None:
            resources.append(resource)
    return resources, warnings


def _resource_from_docker_row(
    resource_kind: str,
    row: Mapping[str, Any],
    *,
    project_to_workspace: Mapping[str, str],
) -> ManagedResource | None:
    project = _extract_project(row)
    name = _row_text(row, "name", "Name")
    resource_id = _row_text(row, "id", "ID") or name
    workspace_id = workspace_id_from_project(project, project_to_workspace=project_to_workspace)

    if workspace_id is None and resource_kind == "volume" and not project:
        workspace_id = _workspace_id_from_managed_name(
            name,
            known_workspace_ids=project_to_workspace.values(),
        )
        if workspace_id is not None:
            project = _infer_project_from_managed_name(name, workspace_id)

    if workspace_id is None:
        return None

    detail: dict[str, str] = {}
    for key in (
        "service",
        "state",
        "status",
        "command",
        "driver",
        "scope",
        "mountpoint",
    ):
        value = _row_text(row, key, key.title())
        if value:
            detail[key] = value
    return ManagedResource(
        resource_kind=resource_kind,
        resource_id=resource_id,
        resource_name=name or resource_id,
        workspace_id=workspace_id,
        compose_project=project or None,
        detail=detail,
    )


def _extract_project(row: Mapping[str, Any]) -> str:
    project = _row_text(row, "project", "Project", "compose_project", "ComposeProject")
    if project:
        return project
    labels = row.get("labels", row.get("Labels"))
    return _label_value(labels, "com.docker.compose.project")


def _label_value(labels: Any, key: str) -> str:
    if isinstance(labels, Mapping):
        value = labels.get(key)
        return str(value) if value is not None else ""
    if isinstance(labels, str):
        for item in labels.split(","):
            label_key, _separator, label_value = item.partition("=")
            if label_key.strip() == key:
                return label_value.strip()
    return ""


def _row_text(row: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value is not None:
            return str(value)
    return ""


def _scan_worktrees(work_dir: Path) -> tuple[list[ManagedResource], list[ResourceFinding]]:
    root = work_dir / "git" / "worktrees"
    if not root.exists():
        return [], []
    try:
        children = sorted(root.iterdir(), key=lambda child: child.name)
    except OSError as exc:
        return [], [
            _warning(
                resource_kind="worktree",
                reason="WORKTREE_SCAN_FAILED",
                detail=_truncate(str(exc)),
            )
        ]

    resources: list[ManagedResource] = []
    for child in children:
        if child.is_symlink() or not child.is_dir() or not child.name.startswith("ws_"):
            continue
        resources.append(
            ManagedResource(
                resource_kind="worktree",
                resource_id=str(child),
                resource_name=child.name,
                workspace_id=parent_workspace_id_from_companion_worktree_id(child.name)
                or child.name,
                detail={"path": str(child)},
            )
        )
    return resources, []


def _classify_resource(
    resource: ManagedResource,
    *,
    workspace_view: WorkspaceIdView,
    workspaces: Mapping[str, WorkspaceLifecycleSnapshot],
    now: datetime,
    min_retention_hours: float,
) -> ResourceFinding:
    if not workspace_view.available:
        return _finding(resource, classification="unknown", reason="DB_UNAVAILABLE")

    workspace_id = resource.workspace_id
    snapshot = workspaces.get(workspace_id or "")
    if workspace_id is None or snapshot is None:
        return _finding(resource, classification="cleanup_ready", reason="WORKSPACE_MISSING")

    if snapshot.status in ACTIVE_WORKSPACE_STATUSES:
        return _finding(resource, classification="expected", reason="WORKSPACE_ACTIVE")

    if snapshot.status in TERMINAL_WORKSPACE_STATUSES:
        # Containers and networks for a terminal workspace are leaked runtime,
        # not salvage evidence — surface them even within retention so operators
        # see the leak and the worker sweep can release them. Volumes and
        # worktrees ARE the salvage evidence; keep them retained while within
        # retention. Mirrors awf.service.orphan_resources._classify so readyz
        # and `awf service status` agree on what counts as a runtime leak.
        if resource.resource_kind in {"container", "network"}:
            return _finding(
                resource,
                classification="cleanup_ready",
                reason="WORKSPACE_TERMINAL_LIVE_RUNTIME",
            )
        if _within_retention(snapshot.updated_at, now=now, min_retention_hours=min_retention_hours):
            return _finding(
                resource,
                classification="retained",
                reason="WORKSPACE_TERMINAL_WITHIN_RETENTION",
            )
        return _finding(
            resource,
            classification="cleanup_ready",
            reason="WORKSPACE_TERMINAL_RETENTION_EXPIRED",
        )

    return _finding(resource, classification="cleanup_ready", reason="WORKSPACE_STATUS_UNKNOWN")


def _finding(
    resource: ManagedResource,
    *,
    classification: str,
    reason: str,
) -> ResourceFinding:
    return ResourceFinding(
        resource_kind=resource.resource_kind,
        resource_id=resource.resource_id,
        resource_name=resource.resource_name,
        workspace_id=resource.workspace_id,
        compose_project=resource.compose_project,
        reason=reason,
        classification=classification,
        suggested_action=_suggested_action(resource, reason=reason),
        detail=_detail_text(resource.detail),
    )


def _warning(*, resource_kind: str, reason: str, detail: str) -> ResourceFinding:
    return ResourceFinding(
        resource_kind=resource_kind,
        resource_id="",
        resource_name="",
        workspace_id=None,
        compose_project=None,
        reason=reason,
        classification="warning",
        suggested_action=_warning_action(reason),
        detail=detail,
    )


def _suggested_action(resource: ManagedResource, *, reason: str) -> str:
    if reason == "DB_UNAVAILABLE":
        return "Re-run `awf service status` once the control-plane database is reachable."
    if resource.resource_kind == "worktree":
        return (
            "Confirm the workspace is no longer active, then use the existing service GC "
            "path to reclaim the managed worktree."
        )
    project_detail = (
        f" for Compose project `{resource.compose_project}`" if resource.compose_project else ""
    )
    return (
        "Confirm the workspace is no longer active, then use the existing explicit cleanup "
        f"path{project_detail}."
    )


def _warning_action(reason: str) -> str:
    if reason in {"DOCKER_CLI_NOT_FOUND", "DOCKER_UNAVAILABLE", "DOCKER_SCAN_SKIPPED"}:
        return "Start Docker or fix Docker CLI access, then re-run `awf service status`."
    if reason == "WORKTREE_SCAN_FAILED":
        return "Fix permissions on the AWF worktree root, then re-run `awf service status`."
    return "Re-run `awf service status`; if it persists, inspect Docker CLI formatting output."


def _workspace_snapshots(
    workspace_view: WorkspaceIdView,
) -> dict[str, WorkspaceLifecycleSnapshot]:
    snapshots = {snapshot.workspace_id: snapshot for snapshot in workspace_view.snapshots}
    for workspace_id in workspace_view.active_ids:
        snapshots.setdefault(
            workspace_id,
            WorkspaceLifecycleSnapshot(
                workspace_id=workspace_id,
                status=WorkspaceStatus.running.value,
                updated_at=None,
                compose_project_name=f"awf_{workspace_id}",
            ),
        )
    for workspace_id in workspace_view.terminal_ids:
        snapshots.setdefault(
            workspace_id,
            WorkspaceLifecycleSnapshot(
                workspace_id=workspace_id,
                status=WorkspaceStatus.completed.value,
                # Synthetic terminal snapshots have no DB freshness boundary.
                # _within_retention treats this as retention-expired.
                updated_at=None,
                compose_project_name=f"awf_{workspace_id}",
            ),
        )
    return snapshots


def _compose_project_index(
    workspaces: Mapping[str, WorkspaceLifecycleSnapshot],
) -> dict[str, str]:
    projects: dict[str, str] = {}
    for snapshot in workspaces.values():
        if snapshot.compose_project_name:
            projects[snapshot.compose_project_name] = snapshot.workspace_id
        projects.setdefault(f"awf_{snapshot.workspace_id}", snapshot.workspace_id)
        projects.setdefault(f"awf-{snapshot.workspace_id}", snapshot.workspace_id)
    return projects


def _kind_counts(resources: list[ManagedResource]) -> dict[str, int]:
    counts = dict.fromkeys(_RESOURCE_KINDS, 0)
    counts.update(Counter(resource.resource_kind for resource in resources))
    return counts


def _nonzero_counts(counts: Mapping[str, int]) -> dict[str, int]:
    return {kind: count for kind, count in counts.items() if count}


def _workspace_id_from_managed_name(
    name: str,
    *,
    known_workspace_ids: Iterable[str] = (),
) -> str | None:
    tail = _managed_name_tail(name)
    if tail is None:
        return None

    workspace_id = _known_workspace_id_from_managed_tail(tail, known_workspace_ids)
    if workspace_id is not None:
        return workspace_id

    workspace_id = _legacy_workspace_id_from_managed_tail(tail)
    return (
        workspace_id
        if workspace_id is not None and _looks_like_workspace_id(workspace_id)
        else None
    )


def _managed_name_tail(name: str) -> str | None:
    for prefix in _AWF_PROJECT_PREFIXES:
        if name.startswith(prefix):
            return name.removeprefix(prefix)
    return None


def _known_workspace_id_from_managed_tail(
    tail: str,
    known_workspace_ids: Iterable[str],
) -> str | None:
    matches = [
        workspace_id
        for workspace_id in set(known_workspace_ids)
        if _looks_like_workspace_id(workspace_id)
        and (
            tail == workspace_id
            or tail.startswith(f"{workspace_id}_")
            or tail.startswith(f"{workspace_id}-")
        )
    ]
    return max(matches, key=len, default=None)


def _legacy_workspace_id_from_managed_tail(tail: str) -> str | None:
    match = _HYPHEN_DELIMITED_MANAGED_TAIL_RE.match(tail)
    if match is not None:
        return match.group("workspace_id")

    if not tail.startswith("ws_"):
        return None

    suffix = tail.removeprefix("ws_")
    workspace_suffix = ""
    for char in suffix:
        if not char.isalnum():
            break
        workspace_suffix += char
    if not workspace_suffix:
        return None
    return f"ws_{workspace_suffix}"


def _infer_project_from_managed_name(name: str, workspace_id: str) -> str:
    if name.startswith("awf-"):
        return f"awf-{workspace_id}"
    return f"awf_{workspace_id}"


def _looks_like_workspace_id(value: str) -> bool:
    return bool(_WORKSPACE_ID_RE.match(value))


def _within_retention(
    updated_at: datetime | None,
    *,
    now: datetime,
    min_retention_hours: float,
) -> bool:
    if updated_at is None:
        # Missing timestamps are only used by synthetic snapshots. Do not
        # retain terminal resources indefinitely when no freshness is known.
        return False
    return _to_utc(updated_at) > now - timedelta(hours=min_retention_hours)


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _detail_text(detail: Mapping[str, str]) -> str | None:
    if not detail:
        return None
    return ", ".join(f"{key}={value}" for key, value in sorted(detail.items()))


def _truncate(value: str, *, limit: int = 240) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"
