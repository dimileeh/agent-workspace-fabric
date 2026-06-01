"""Audit and setup-dependency event helpers for monitor handoff flows."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from awf.control.executor.constants import (
    _EXECUTOR_AUDIT_ACTOR,
    SETUP_DEPENDENCY_NETWORK_RETRY_EVENT_TYPE,
    SETUP_DEPENDENCY_NETWORK_RETRY_EXHAUSTED_EVENT_TYPE,
)
from awf.control.executor.logging_ops import (
    _setup_dependency_network_details,
    _setup_dependency_network_event_payload,
)
from awf.control.executor.metadata import _metadata_int
from awf.db.models import Workspace
from awf.db.repositories import WorkspaceRepository
from awf.runtime.validation import (
    SETUP_DEPENDENCY_NETWORK_RETRY,
    SETUP_DEPENDENCY_NETWORK_RETRY_EXHAUSTED,
    ValidationResult,
)


async def _record_executor_pr_audit_event(
    self: Any,
    workspace_id: str,
    *,
    event_type: str,
    action: str,
    outcome: str,
    reason_code: str,
    branch_name: str | None = None,
    remote_branch: str | None = None,
    pr_number: int | None = None,
    pr_url: str | None = None,
    source_head_sha: str | None = None,
    source_base_sha: str | None = None,
    operation_id: str | None = None,
    operation_type: str | None = None,
    evidence: Mapping[str, Any] | None = None,
) -> None:
    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.get(workspace_id)
        if workspace is None:  # pragma: no cover - destroyed mid-flight
            return
        await self._add_executor_pr_audit_event(
            repo,
            workspace,
            event_type=event_type,
            action=action,
            outcome=outcome,
            reason_code=reason_code,
            branch_name=branch_name,
            remote_branch=remote_branch,
            pr_number=pr_number,
            pr_url=pr_url,
            source_head_sha=source_head_sha,
            source_base_sha=source_base_sha,
            operation_id=operation_id,
            operation_type=operation_type,
            evidence=evidence,
        )
        await session.commit()


async def _add_executor_pr_audit_event(
    self: Any,
    repo: WorkspaceRepository,
    workspace: Workspace,
    *,
    event_type: str,
    action: str,
    outcome: str,
    reason_code: str,
    branch_name: str | None = None,
    remote_branch: str | None = None,
    pr_number: int | None = None,
    pr_url: str | None = None,
    source_head_sha: str | None = None,
    source_base_sha: str | None = None,
    operation_id: str | None = None,
    operation_type: str | None = None,
    evidence: Mapping[str, Any] | None = None,
) -> None:
    _ = self
    resolved_branch_name = branch_name or workspace.branch_name
    resolved_remote_branch = remote_branch or workspace.remote_push_branch or workspace.branch_name
    await repo.add_audit_event(
        workspace,
        event_type=event_type,
        actor=_EXECUTOR_AUDIT_ACTOR,
        action=action,
        outcome=outcome,
        reason_code=reason_code,
        operation_id=operation_id,
        operation_type=operation_type,
        pr_number=pr_number if pr_number is not None else workspace.pr_number,
        pr_url=pr_url or workspace.pr_url,
        # Preserve a commit reference on audit records even when the caller
        # does not supply a source head explicitly.
        source_head_sha=source_head_sha or workspace.monitor_last_commit_sha,
        source_base_sha=source_base_sha or workspace.base_commit,
        target_branch=workspace.branch_base,
        remote_branch=resolved_remote_branch,
        branch_name=resolved_branch_name,
        evidence=evidence,
    )


async def _record_setup_dependency_network_events(
    self: Any,
    *,
    workspace_id: str,
    result: ValidationResult,
) -> None:
    event_specs: list[tuple[str, str, dict[str, Any]]] = []
    commands = getattr(result, "commands", None)
    if not commands:
        return
    for command in commands:
        details = _setup_dependency_network_details(command)
        if details is None:
            continue
        retry_count = _metadata_int(details, "retry_count") or 0
        if retry_count > 0:
            # Exhausted attempts intentionally emit both the retry event and
            # the exhausted event from the same redacted retry metadata.
            event_specs.append(
                (
                    SETUP_DEPENDENCY_NETWORK_RETRY_EVENT_TYPE,
                    SETUP_DEPENDENCY_NETWORK_RETRY,
                    _setup_dependency_network_event_payload(
                        details,
                        reason_code=SETUP_DEPENDENCY_NETWORK_RETRY,
                    ),
                )
            )
        if details.get("retry_exhausted") is True:
            event_specs.append(
                (
                    SETUP_DEPENDENCY_NETWORK_RETRY_EXHAUSTED_EVENT_TYPE,
                    SETUP_DEPENDENCY_NETWORK_RETRY_EXHAUSTED,
                    _setup_dependency_network_event_payload(
                        details,
                        reason_code=SETUP_DEPENDENCY_NETWORK_RETRY_EXHAUSTED,
                    ),
                )
            )
    if not event_specs:
        return

    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.get(workspace_id)
        if workspace is None:  # pragma: no cover - destroyed mid-flight
            return
        for event_type, reason_code, payload in event_specs:
            await repo.add_event(
                workspace,
                event_type=event_type,
                reason_code=reason_code,
                payload=payload,
            )
        await session.commit()
