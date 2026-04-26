"""Validation provenance endpoints."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from awf.api.deps import get_db_session
from awf.api.schemas import (
    ValidationProvenanceItemResponse,
    ValidationProvenanceListResponse,
    ValidationProvenanceStatus,
)
from awf.db.enums import FailureReason, WorkspaceStatus
from awf.db.models import Workspace, WorkspaceLogStream
from awf.db.repositories import WorkspaceLogStreamRepository, WorkspaceRepository
from awf.profiles.models import WorkspaceProfile

router = APIRouter(prefix="/v1/workspaces/{workspace_id}/validation", tags=["validation"])

_LABEL_RE = re.compile(r"^(?P<index>\d+)_(?P<phase>[A-Za-z][A-Za-z0-9_-]*)$")
_LEGACY_LABEL_RE = re.compile(r"^cmd_(?P<index>\d+)$")
_TRAILING_NUMBER_RE = re.compile(r"(?P<index>\d+)$")

_PHASE_ORDER = {
    "setup": 0,
    "pre_agent": 1,
    "healthcheck": 2,
    "post_agent": 3,
    "validate": 4,
    "cleanup": 5,
    "unknown": 99,
}
_SUCCESS_WORKSPACE_STATUSES = {
    WorkspaceStatus.pushing.value,
    WorkspaceStatus.monitoring_pr.value,
    WorkspaceStatus.completed.value,
}
_VALIDATION_FAILURE_REASONS = {
    FailureReason.validation_failure.value,
    FailureReason.phase_timeout.value,
    FailureReason.health_check_failure.value,
    FailureReason.service_startup_failure.value,
}


@router.get("", response_model=ValidationProvenanceListResponse)
async def list_validation_provenance(
    workspace_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> ValidationProvenanceListResponse:
    workspace = await WorkspaceRepository(session).get(workspace_id)
    if workspace is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "NOT_FOUND", "message": f"No workspace with id {workspace_id}"},
        )

    streams = await WorkspaceLogStreamRepository(session).list_validation_for_workspace(
        workspace_id
    )
    return ValidationProvenanceListResponse(items=_build_validation_items(workspace, streams))


@dataclass
class _StreamPair:
    base_stream_id: str
    stdout: WorkspaceLogStream | None = None
    stderr: WorkspaceLogStream | None = None

    def add(self, fd: str, stream: WorkspaceLogStream) -> None:
        if fd == "stdout":
            self.stdout = stream
        elif fd == "stderr":
            self.stderr = stream

    def streams(self) -> list[WorkspaceLogStream]:
        return [stream for stream in (self.stdout, self.stderr) if stream is not None]


@dataclass(frozen=True)
class _CommandRecord:
    pair: _StreamPair
    phase: str
    command_index: int
    command: str | None

    @property
    def sort_key(self) -> tuple[int, int, str, str]:
        return (
            _PHASE_ORDER.get(self.phase, _PHASE_ORDER["unknown"]),
            self.command_index,
            self.phase,
            self.pair.base_stream_id,
        )


def _build_validation_items(
    workspace: Workspace,
    streams: list[WorkspaceLogStream],
) -> list[ValidationProvenanceItemResponse]:
    command_lookup = _command_lookup(workspace)
    records = [_command_record(pair, command_lookup) for pair in _group_streams(streams).values()]
    records.sort(key=lambda record: record.sort_key)
    failed_record = _failed_record(workspace, records)
    return [
        ValidationProvenanceItemResponse(
            workspace_id=workspace.id,
            phase=record.phase,
            command_index=record.command_index,
            command=record.command,
            stream_ids={
                "stdout": record.pair.stdout.stream_id if record.pair.stdout else None,
                "stderr": record.pair.stderr.stream_id if record.pair.stderr else None,
            },
            stdout_byte_count=record.pair.stdout.byte_count if record.pair.stdout else 0,
            stdout_line_count=record.pair.stdout.line_count if record.pair.stdout else 0,
            stderr_byte_count=record.pair.stderr.byte_count if record.pair.stderr else 0,
            stderr_line_count=record.pair.stderr.line_count if record.pair.stderr else 0,
            opened_at=_opened_at(record.pair),
            closed_at=_closed_at(record.pair),
            status=_record_status(workspace, record, failed_record),
            base_commit=workspace.base_commit,
            branch_name=workspace.branch_name,
        )
        for record in records
    ]


def _group_streams(streams: list[WorkspaceLogStream]) -> dict[str, _StreamPair]:
    grouped: dict[str, _StreamPair] = {}
    for stream in streams:
        fd = _stream_fd(stream)
        if fd is None:
            continue
        base_stream_id = _base_stream_id(stream.stream_id)
        pair = grouped.setdefault(base_stream_id, _StreamPair(base_stream_id=base_stream_id))
        pair.add(fd, stream)
    return grouped


def _stream_fd(stream: WorkspaceLogStream) -> str | None:
    if stream.kind in {"stdout", "stderr"}:
        return stream.kind
    if stream.stream_id.endswith(".stdout"):
        return "stdout"
    if stream.stream_id.endswith(".stderr"):
        return "stderr"
    return None


def _base_stream_id(stream_id: str) -> str:
    for suffix in (".stdout", ".stderr"):
        if stream_id.endswith(suffix):
            return stream_id[: -len(suffix)]
    return stream_id


def _command_record(
    pair: _StreamPair,
    command_lookup: dict[tuple[str, int], str],
) -> _CommandRecord:
    phase, command_index = _phase_and_index(pair)
    return _CommandRecord(
        pair=pair,
        phase=phase,
        command_index=command_index,
        command=command_lookup.get((phase, command_index)),
    )


def _phase_and_index(pair: _StreamPair) -> tuple[str, int]:
    label = _label(pair.base_stream_id)
    if match := _LABEL_RE.match(label):
        return _normalize_phase(match.group("phase")), int(match.group("index"))
    if match := _LEGACY_LABEL_RE.match(label):
        return _phase_from_stream_name(pair) or "validate", int(match.group("index"))
    if match := _TRAILING_NUMBER_RE.search(label):
        return _phase_from_stream_name(pair) or "unknown", int(match.group("index"))
    return _phase_from_stream_name(pair) or "unknown", 0


def _label(base_stream_id: str) -> str:
    for prefix in ("validation.", "setup."):
        if base_stream_id.startswith(prefix):
            return base_stream_id[len(prefix) :]
    return base_stream_id.rsplit(".", maxsplit=1)[-1]


def _phase_from_stream_name(pair: _StreamPair) -> str | None:
    for stream in pair.streams():
        first = stream.name.split(maxsplit=1)[0].strip()
        phase = _normalize_phase(first)
        if phase in _PHASE_ORDER and phase != "unknown":
            return phase
    return None


def _normalize_phase(value: str) -> str:
    return value.strip().lower().replace("-", "_")


def _command_lookup(workspace: Workspace) -> dict[tuple[str, int], str]:
    lookup: dict[tuple[str, int], str] = {}
    profile = _resolved_profile(workspace)
    if profile is not None:
        for phase_name in (
            "setup",
            "pre_agent",
            "healthcheck",
            "post_agent",
            "validate",
            "cleanup",
        ):
            phase_key = _normalize_phase(phase_name)
            if phase_name == "healthcheck":
                for index, healthcheck in enumerate(
                    profile.validation.healthchecks,
                    start=1,
                ):
                    lookup.setdefault((phase_key, index), healthcheck.command)
                continue

            for index, (_, command) in enumerate(
                profile.phases.commands_for((phase_name,)),
                start=1,
            ):
                lookup.setdefault((phase_key, index), command.command)

    for index, test_command in enumerate(workspace.test_commands, start=1):
        lookup.setdefault(("validate", index), test_command)

    return lookup


def _resolved_profile(workspace: Workspace) -> WorkspaceProfile | None:
    if not isinstance(workspace.resolved_profile, dict):
        return None
    try:
        return WorkspaceProfile.model_validate(workspace.resolved_profile)
    except ValueError:
        return None


def _opened_at(pair: _StreamPair) -> datetime:
    return _ensure_utc(min(stream.opened_at for stream in pair.streams()))


def _closed_at(pair: _StreamPair) -> datetime | None:
    streams = pair.streams()
    closed = [stream.closed_at for stream in streams]
    if any(value is None for value in closed):
        return None
    latest = max(value for value in closed if value is not None)
    return _ensure_utc(latest)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _record_status(
    workspace: Workspace,
    record: _CommandRecord,
    failed_record: _CommandRecord | None,
) -> ValidationProvenanceStatus:
    if _closed_at(record.pair) is None:
        return "running"
    if workspace.status in _SUCCESS_WORKSPACE_STATUSES:
        return "succeeded"
    if workspace.status == WorkspaceStatus.failed.value and failed_record is not None:
        if record is failed_record:
            return "failed"
        if record.sort_key < failed_record.sort_key:
            return "succeeded"
    return "unknown"


def _failed_record(
    workspace: Workspace,
    records: list[_CommandRecord],
) -> _CommandRecord | None:
    if (
        workspace.status != WorkspaceStatus.failed.value
        or workspace.failure_reason not in _VALIDATION_FAILURE_REASONS
    ):
        return None
    message = (workspace.failure_message or "").lower()
    if not message:
        return None

    for record in records:
        if record.command is not None and record.command.lower() in message:
            return record

    matches = [record for record in records if record.phase in message]
    return matches[0] if len(matches) == 1 else None
