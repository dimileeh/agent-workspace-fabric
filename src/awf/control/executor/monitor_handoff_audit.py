"""Audit and setup-dependency event helpers for monitor handoff flows."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from awf.common.logging import get_logger
from awf.control.executor.constants import (
    _EXECUTOR_AUDIT_ACTOR,
    RUNTIME_TOOLCHAIN_UNAVAILABLE_EVENT_TYPE,
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
from awf.profiles.models import RUNTIME_TOOLCHAIN_UNAVAILABLE
from awf.runtime.validation import (
    SETUP_DEPENDENCY_NETWORK_RETRY,
    SETUP_DEPENDENCY_NETWORK_RETRY_EXHAUSTED,
    ValidationResult,
)

_log = get_logger(__name__)

_UNSET_SOURCE_HEAD_SHA = object()


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
    source_head_sha: str | None | object = _UNSET_SOURCE_HEAD_SHA,
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
    source_head_sha: str | None | object = _UNSET_SOURCE_HEAD_SHA,
    source_base_sha: str | None = None,
    operation_id: str | None = None,
    operation_type: str | None = None,
    evidence: Mapping[str, Any] | None = None,
) -> None:
    _ = self
    resolved_branch_name = branch_name or workspace.branch_name
    resolved_remote_branch = remote_branch or workspace.remote_push_branch or workspace.branch_name
    # Omitted source heads get the workspace's last known monitor commit; an
    # explicit None remains null so genuinely SHA-less events stay visible.
    resolved_source_head_sha = (
        workspace.monitor_last_commit_sha
        if source_head_sha is _UNSET_SOURCE_HEAD_SHA
        else cast(str | None, source_head_sha)
    )
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
        source_head_sha=resolved_source_head_sha,
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


async def _record_runtime_toolchain_findings(
    self: Any,
    *,
    workspace_id: str,
    compose_project: str,
    compose_file: Any,
    profile: Any,
) -> None:
    """Probe the container for declared toolchains; record warnings (non-blocking).

    Runs the provision-time toolchain probe via the validation runner and
    appends one ``RUNTIME_TOOLCHAIN_UNAVAILABLE`` warning event per finding. The
    probe is strictly additive: any failure is swallowed here so it can never
    affect provisioning or a state transition. Legacy ``_validation`` stubs that
    predate the probe method are a no-op (the ``getattr`` returns ``None``).
    """
    probe = getattr(self._validation, "probe_runtime_toolchain_findings", None)
    if not callable(probe):
        return
    try:
        findings = await probe(
            workspace_id=workspace_id,
            compose_project=compose_project,
            compose_file=compose_file,
            profile=profile,
        )
    except Exception:
        _log.exception(
            "executor.runtime_toolchain_probe_failed",
            workspace_id=workspace_id,
        )
        return
    if not findings:
        return

    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.get(workspace_id)
        if workspace is None:  # pragma: no cover - destroyed mid-flight
            return
        for finding in findings:
            details = dict(finding.details)
            await repo.add_event(
                workspace,
                event_type=RUNTIME_TOOLCHAIN_UNAVAILABLE_EVENT_TYPE,
                reason_code=RUNTIME_TOOLCHAIN_UNAVAILABLE,
                payload={
                    "language": details.get("language"),
                    "version": details.get("version"),
                    "available_versions": details.get("available_versions"),
                    "path": finding.path,
                    "message": finding.message,
                },
            )
        await session.commit()
