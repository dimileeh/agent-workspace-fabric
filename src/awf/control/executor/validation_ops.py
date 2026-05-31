"""Extracted WorkspaceExecutor domain operations.

This module contains mechanically moved methods from ``awf.control.executor.base`` and keeps behavior unchanged.
"""

from __future__ import annotations

import asyncio as asyncio
import hashlib as hashlib
import json as json
import re as re
import shlex as shlex
import time as time
import traceback as traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession

from awf.common.audit import redact_audit_text
from awf.control.executor.helpers import (
    _validation_run_command_records,
)
from awf.control.executor.logging_ops import (
    _validation_run_log_stream_refs,
)
from awf.control.executor.quality_gates import (
    _log,
)
from awf.control.executor.status_helpers import _is_callback_terminal_status
from awf.db.enums import (
    OperationStatus,
    OperationType,
    WorkspaceStatus,
)
from awf.db.repositories import (
    OperationRepository,
    TaskAttemptRepository,
    ValidationRunRepository,
    WorkspaceRepository,
)
from awf.profiles.models import WorkspaceProfile
from awf.runtime.validation_identity import (
    environment_identity_digest,
    environment_identity_inputs,
    resolved_profile_digest,
)


async def _start_validation_run(
    self: Any,
    *,
    workspace_id: str,
    profile: WorkspaceProfile,
    base_commit: str | None,
    workspace_head_sha: str | None,
    target_branch: str | None,
    target_head_sha: str | None,
    tier: int,
    coverage_evidence_status: str | None = None,
    coverage_evidence_reason_code: str | None = None,
) -> str:
    command_records = _validation_run_command_records(
        profile=profile,
        phase_names=("post_agent", "validate"),
        run_healthchecks=True,
        coverage_evidence_status=coverage_evidence_status,
        coverage_evidence_reason_code=coverage_evidence_reason_code,
    )
    async with self._session_factory() as session:
        attempt = await TaskAttemptRepository(session).get_by_workspace_id(workspace_id)
        run = await ValidationRunRepository(session).start(
            workspace_id=workspace_id,
            attempt_id=attempt.id if attempt is not None else None,
            tier=tier,
            commands=command_records,
            base_commit=base_commit,
            base_sha=base_commit,
            workspace_head_sha=workspace_head_sha,
            target_branch=target_branch,
            target_head_sha=target_head_sha,
            profile_name=profile.name,
            profile_version=profile.version,
            profile_source=profile.source,
            resolved_profile_digest=resolved_profile_digest(profile),
            environment_identity_digest=environment_identity_digest(profile),
            environment_identity_inputs=environment_identity_inputs(profile),
            log_stream_refs=_validation_run_log_stream_refs(command_records),
            started_at=datetime.now(UTC),
        )
        await session.commit()
        return run.id


async def _capture_workspace_head_sha(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
) -> str | None:
    result = await self._runner.run(
        ["git", "-C", str(worktree_path), "rev-parse", "HEAD"],
    )
    head_sha = result.stdout.strip()
    if result.ok and head_sha:
        return cast(str, head_sha)
    _log.warning(
        "executor.validation_workspace_head_sha_capture_failed",
        workspace_id=workspace_id,
        returncode=result.returncode,
        stderr=result.stderr[:400],
    )
    return None


async def _finish_pending_validate_operations(
    self: Any,
    *,
    workspace_id: str,
    status: OperationStatus,
    validation_run_id: str | None,
    requested_tier: int,
    reason_code: str | None,
    error_message: str | None = None,
    coverage: dict[str, object] | None = None,
) -> None:
    async with self._session_factory() as session:
        await self._finish_pending_validate_operations_in_session(
            session,
            workspace_id=workspace_id,
            status=status,
            validation_run_id=validation_run_id,
            requested_tier=requested_tier,
            reason_code=reason_code,
            error_message=error_message,
            coverage=coverage,
        )
        await session.commit()


async def _finish_pending_validate_operations_in_session(
    self: Any,
    session: AsyncSession,
    *,
    workspace_id: str,
    status: OperationStatus,
    validation_run_id: str | None,
    requested_tier: int,
    reason_code: str | None,
    error_message: str | None = None,
    coverage: dict[str, object] | None = None,
) -> None:
    _ = self
    repo = OperationRepository(session)
    pending = await repo.list_for_workspace(
        workspace_id,
        operation_type=OperationType.validate,
        status=OperationStatus.pending,
        limit=100,
    )
    running = await repo.list_for_workspace(
        workspace_id,
        operation_type=OperationType.validate,
        status=OperationStatus.running,
        limit=100,
    )
    result: dict[str, object] = {
        "validation_run_id": validation_run_id,
        "requested_tier": requested_tier,
        "reason_code": reason_code,
    }
    validation_run = (
        await ValidationRunRepository(session).get(validation_run_id)
        if validation_run_id is not None
        else None
    )
    if validation_run is not None and isinstance(validation_run.log_stream_refs, dict):
        result["log_stream_refs"] = dict(validation_run.log_stream_refs)
    if coverage is not None:
        result["coverage"] = coverage
    safe_error_message = redact_audit_text(error_message) if error_message is not None else None
    for operation in [*pending, *running]:
        payload = dict(operation.payload or {})
        payload.setdefault("requested_tier", requested_tier)
        operation.payload = payload
        await repo.finish(
            operation,
            status=status,
            result=result,
            error_code=reason_code if status == OperationStatus.failed else None,
            error_message=safe_error_message,
        )


async def _finish_validation_run(
    self: Any,
    validation_run_id: str,
    *,
    status: str,
    reason_code: str | None,
    retry_count: int = 0,
    coverage: dict[str, object] | None = None,
    command_retries: list[int] | None = None,
    coverage_evidence_status: str | None = None,
    coverage_evidence_reason_code: str | None = None,
    coverage_evidence_source_run_id: str | None = None,
) -> None:
    async with self._session_factory() as session:
        await ValidationRunRepository(session).finish(
            validation_run_id,
            status=status,
            reason_code=reason_code,
            finished_at=datetime.now(UTC),
            retry_count=retry_count,
            coverage=coverage,
            command_retries=command_retries,
            coverage_evidence_status=coverage_evidence_status,
            coverage_evidence_reason_code=coverage_evidence_reason_code,
            coverage_evidence_source_run_id=coverage_evidence_source_run_id,
        )
        await session.commit()


async def _finish_validation_callback_if_terminal(
    self: Any,
    *,
    workspace_id: str,
    validation_run_id: str,
    requested_tier: int,
) -> bool:
    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get(workspace_id)
        if ws is None:  # pragma: no cover - row disappeared mid-validation
            return True
        if ws.status == WorkspaceStatus.validating.value:
            return False
        if not _is_callback_terminal_status(ws.status):
            return False
        await self._record_stale_action_skip(
            repo,
            ws,
            action="validate",
            expected=WorkspaceStatus.validating,
            reason_code="STALE_CALLBACK_IGNORED",
        )
        await ValidationRunRepository(session).finish(
            validation_run_id,
            status="failed",
            reason_code="STALE_CALLBACK_IGNORED",
            finished_at=datetime.now(UTC),
        )
        await self._finish_ignored_stale_callback_operations_in_session(
            session,
            workspace_id=workspace_id,
            callback_source="executor",
            callback_action="validate",
            expected_status=WorkspaceStatus.validating,
            actual_status=ws.status,
            validation_run_id=validation_run_id,
            requested_tier=requested_tier,
        )
        await session.commit()
        return True


async def _set_validation_run_target_head_sha(
    self: Any,
    *,
    validation_run_id: str,
    target_head_sha: str,
    workspace_head_sha: str | None = None,
) -> None:
    async with self._session_factory() as session:
        await ValidationRunRepository(session).update_target_head_sha(
            validation_run_id,
            target_head_sha=target_head_sha,
            workspace_head_sha=workspace_head_sha,
        )
        await session.commit()
