"""Shared validation provenance builders for REST and MCP."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession

from awf.api.schemas import (
    ValidationProvenanceItemResponse,
    ValidationProvenanceListResponse,
    ValidationProvenanceStatus,
)
from awf.api.validation_runs import (
    _json_dict,
    _validation_status,
    _validation_tier,
    fresh_for_target,
    validation_coverage_fields,
    validation_identity_fields,
)
from awf.db.enums import FailureReason, WorkspaceStatus
from awf.db.models import ValidationRun, Workspace, WorkspaceLogStream
from awf.db.repositories import (
    ValidationRunRepository,
    WorkspaceLogStreamRepository,
    WorkspaceRepository,
)
from awf.profiles.models import WorkspaceProfile
from awf.service.bounded_list import (
    BoundedListPage,
    paginate_bounded_iterable,
    paginate_bounded_list,
)

_LABEL_RE = re.compile(r"^(?P<index>\d+)_(?P<phase>[A-Za-z][A-Za-z0-9_-]*)$")
_LEGACY_LABEL_RE = re.compile(r"^cmd_(?P<index>\d+)$")
_TRAILING_NUMBER_RE = re.compile(r"(?P<index>\d+)$")

_PHASE_ORDER = {
    "setup": 0,
    "db_generated_setup": 1,
    "pre_agent": 2,
    "healthcheck": 3,
    "post_agent": 4,
    "db_refresh": 5,
    "validate": 6,
    "coverage": 7,
    "cleanup": 8,
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
DEFAULT_VALIDATION_PROVENANCE_LIMIT = 50
MAX_VALIDATION_PROVENANCE_LIMIT = 500


async def list_validation_provenance_response(
    session: AsyncSession,
    *,
    workspace_id: str,
    limit: int = DEFAULT_VALIDATION_PROVENANCE_LIMIT,
    cursor: str | None = None,
) -> ValidationProvenanceListResponse | None:
    workspace = await WorkspaceRepository(session).get(workspace_id)
    if workspace is None:
        return None

    stream_repo = WorkspaceLogStreamRepository(session)
    validation_runs = await ValidationRunRepository(session).list_for_workspace(workspace_id)
    streams = await stream_repo.list_validation_for_workspace(workspace_id)
    if validation_runs:
        page = _build_persisted_validation_items_page(
            workspace,
            validation_runs,
            streams,
            limit=limit,
            cursor=cursor,
        )
    else:
        page = _build_validation_items_page(
            workspace,
            streams,
            limit=limit,
            cursor=cursor,
        )
    return ValidationProvenanceListResponse(
        items=page.items,
        next_cursor=page.next_cursor,
        has_more=page.has_more,
        limit=page.limit,
        cursor=page.cursor,
    )


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


@dataclass(frozen=True)
class _PersistedCommandRecord:
    run: ValidationRun
    command: dict[str, Any]


def _build_validation_items_page(
    workspace: Workspace,
    streams: list[WorkspaceLogStream],
    *,
    limit: int,
    cursor: str | None,
) -> BoundedListPage[ValidationProvenanceItemResponse]:
    command_lookup = _command_lookup(workspace)
    records = [_command_record(pair, command_lookup) for pair in _group_streams(streams).values()]
    records.sort(key=lambda record: record.sort_key)
    failed_record = _failed_record(workspace, records)
    record_page = paginate_bounded_list(
        records,
        limit=limit,
        max_limit=MAX_VALIDATION_PROVENANCE_LIMIT,
        cursor=cursor,
    )
    return BoundedListPage(
        items=[
            _build_validation_item(workspace, record, failed_record) for record in record_page.items
        ],
        next_cursor=record_page.next_cursor,
        has_more=record_page.has_more,
        limit=record_page.limit,
        cursor=record_page.cursor,
    )


def _build_validation_item(
    workspace: Workspace,
    record: _CommandRecord,
    failed_record: _CommandRecord | None,
) -> ValidationProvenanceItemResponse:
    return ValidationProvenanceItemResponse(
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


def _build_persisted_validation_items_page(
    workspace: Workspace,
    validation_runs: list[ValidationRun],
    streams: list[WorkspaceLogStream],
    *,
    limit: int,
    cursor: str | None,
) -> BoundedListPage[ValidationProvenanceItemResponse]:
    stream_lookup = {stream.stream_id: stream for stream in streams}
    current_target_head_sha = _current_target_head_sha(workspace)
    record_page = paginate_bounded_iterable(
        _iter_persisted_command_records(validation_runs),
        limit=limit,
        max_limit=MAX_VALIDATION_PROVENANCE_LIMIT,
        cursor=cursor,
    )
    return BoundedListPage(
        items=[
            _build_persisted_validation_item(
                workspace,
                record.run,
                record.command,
                stream_lookup,
                current_target_head_sha,
            )
            for record in record_page.items
        ],
        next_cursor=record_page.next_cursor,
        has_more=record_page.has_more,
        limit=record_page.limit,
        cursor=record_page.cursor,
    )


def _iter_persisted_command_records(
    validation_runs: list[ValidationRun],
) -> Iterator[_PersistedCommandRecord]:
    for run in validation_runs:
        commands = _run_commands(run)
        if not commands:
            commands = [
                {
                    "phase": "unknown",
                    "command_index": 0,
                    "command": None,
                    "stream_ids": {},
                }
            ]
        for command in commands:
            yield _PersistedCommandRecord(run=run, command=command)


def _build_persisted_validation_item(
    workspace: Workspace,
    run: ValidationRun,
    command: dict[str, Any],
    stream_lookup: dict[str, WorkspaceLogStream],
    current_target_head_sha: str | None,
) -> ValidationProvenanceItemResponse:
    stream_ids = _command_stream_ids(command)
    stdout = stream_lookup.get(stream_ids.get("stdout") or "")
    stderr = stream_lookup.get(stream_ids.get("stderr") or "")
    return ValidationProvenanceItemResponse(
        validation_run_id=run.id,
        workspace_id=workspace.id,
        attempt_id=run.attempt_id,
        tier=_validation_tier(run.tier),
        command_set_hash=run.command_set_hash,
        phase=_command_phase(command),
        command_index=_command_index(command),
        command=_command_text(command),
        stream_ids=stream_ids,
        stdout_byte_count=stdout.byte_count if stdout else 0,
        stdout_line_count=stdout.line_count if stdout else 0,
        stderr_byte_count=stderr.byte_count if stderr else 0,
        stderr_line_count=stderr.line_count if stderr else 0,
        opened_at=_ensure_utc(run.started_at),
        closed_at=_ensure_utc(run.finished_at) if run.finished_at else None,
        status=_validation_status(run.status),
        reason_code=run.reason_code,
        base_commit=run.base_commit,
        **validation_identity_fields(run),
        branch_name=run.target_branch or workspace.branch_name,
        target_branch=run.target_branch,
        target_head_sha=run.target_head_sha,
        current_target_head_sha=current_target_head_sha,
        started_at=_ensure_utc(run.started_at),
        finished_at=_ensure_utc(run.finished_at) if run.finished_at else None,
        log_stream_refs=_json_dict(run.log_stream_refs),
        fresh_for_target=fresh_for_target(
            validation_target_head_sha=run.target_head_sha,
            current_target_head_sha=current_target_head_sha,
        ),
        retry_count=run.retry_count,
        **validation_coverage_fields(run),
    )


def _current_target_head_sha(workspace: Workspace) -> str | None:
    candidates = sorted(
        workspace.merge_candidates,
        key=lambda candidate: (candidate.updated_at, candidate.id),
        reverse=True,
    )
    for candidate in candidates:
        if candidate.head_sha:
            return candidate.head_sha
    return workspace.monitor_last_commit_sha


def _run_commands(run: ValidationRun) -> list[dict[str, Any]]:
    commands = cast(Any, run.commands)
    if not isinstance(commands, list):
        return []
    return [command for command in commands if isinstance(command, dict)]


def _command_stream_ids(command: dict[str, Any]) -> dict[str, str | None]:
    value = command.get("stream_ids")
    if not isinstance(value, dict):
        return {"stdout": None, "stderr": None}
    stdout = value.get("stdout")
    stderr = value.get("stderr")
    return {
        "stdout": stdout if isinstance(stdout, str) else None,
        "stderr": stderr if isinstance(stderr, str) else None,
    }


def _command_phase(command: dict[str, Any]) -> str:
    value = command.get("phase")
    return _normalize_phase(value) if isinstance(value, str) else "unknown"


def _command_index(command: dict[str, Any]) -> int:
    value = command.get("command_index")
    if isinstance(value, int):
        return value
    return 0


def _command_text(command: dict[str, Any]) -> str | None:
    value = command.get("command")
    return value if isinstance(value, str) else None


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
            "db_generated_setup",
            "pre_agent",
            "healthcheck",
            "post_agent",
            "db_refresh",
            "validate",
            "coverage",
            "cleanup",
        ):
            phase_key = _normalize_phase(phase_name)
            if phase_name == "coverage":
                coverage_command = profile.validation.coverage.command
                if coverage_command is not None:
                    lookup.setdefault((phase_key, 1), coverage_command.command)
                continue
            if phase_name == "healthcheck":
                for index, healthcheck in enumerate(
                    profile.validation.healthchecks,
                    start=1,
                ):
                    lookup.setdefault((phase_key, index), healthcheck.display_command())
                continue
            if phase_name == "db_generated_setup":
                for index, command in enumerate(profile.database.generated_setup, start=1):
                    lookup.setdefault((phase_key, index), command.command)
                continue
            if phase_name == "db_refresh":
                for index, command in enumerate(
                    profile.database.pre_validation_refresh,
                    start=1,
                ):
                    lookup.setdefault((phase_key, index), command.command)
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
        if workspace.status == WorkspaceStatus.failed.value:
            return "failed"
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
    return matches[-1] if matches else None
