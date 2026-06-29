"""Extracted WorkspaceExecutor domain operations.

This module contains mechanically moved methods from ``awf.control.executor.base`` and keeps behavior unchanged.
"""

from __future__ import annotations

import asyncio as asyncio
import hashlib as hashlib
import json as json
import os as os
import re as re
import shlex as shlex
import time as time
import traceback as traceback
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from awf.adapters.base import get_adapter
from awf.common.audit import redact_audit_text, redact_audit_value
from awf.common.bitbucket_client import BitbucketClientError
from awf.common.forge import ForgeNotSupportedError
from awf.control.executor import monitor_handoff_audit as _monitor_handoff_audit
from awf.control.executor import monitor_handoff_companion_env as _companion_env
from awf.control.executor.constants import (
    _DEPRECATED_TASK_KIND_REASON_CODE,
    _PR_ADOPTION_MONITOR_UNAVAILABLE_REASON_CODE,
    _SUPPORTED_TASK_KINDS,
    _UNSUPPORTED_TASK_KIND_REASON_CODE,
)
from awf.control.executor.forge_gate import unsupported_forge_error
from awf.control.executor.helpers import (
    _agent_defaults_for_workspace,
    _call_pr_monitor_factory,
    _missing_monitor_recovery_metadata,
    _profile_for_workspace,
    _provider_recovery_default_model_for_monitor_handoff,
    _redacted_exception_traceback,
)
from awf.control.executor.monitor_handoff_setup import _MonitorHandoffSetupFailureError
from awf.control.executor.protocols import _MonitorRunnerProto
from awf.control.executor.quality_gates import (
    _log,
)
from awf.control.executor.recovery_payloads import _get_active_recovery_payload
from awf.control.executor.state_ops import _sync_resolved_profile
from awf.db.enums import (
    DEPRECATED_MONITOR_RELEASE_PR_TASK_KIND,
    AgentRuntime,
    FailureReason,
    OperationStatus,
    WorkspaceStatus,
)
from awf.db.models import Workspace
from awf.db.repositories import WorkspaceRepository
from awf.node.companion_services import (
    COMPANION_ENV_SECRET_SOURCE_EMPTY,
    COMPANION_ENV_SECRET_SOURCE_MISSING,
    WorkspaceCompanionSpec,
    companion_specs_from_task_policy,
)
from awf.node.compose_manager import ComposeOperationError
from awf.node.stack_launcher import effective_compose_up_timeout_seconds

_add_executor_pr_audit_event = _monitor_handoff_audit._add_executor_pr_audit_event
_record_executor_pr_audit_event = _monitor_handoff_audit._record_executor_pr_audit_event
_record_setup_dependency_network_events = (
    _monitor_handoff_audit._record_setup_dependency_network_events
)
_record_runtime_toolchain_findings = _monitor_handoff_audit._record_runtime_toolchain_findings
_record_runtime_toolchain_findings_safe = (
    _monitor_handoff_audit._record_runtime_toolchain_findings_safe
)
_ComposeInterpolationPreservingDumper = _companion_env._ComposeInterpolationPreservingDumper
_ComposeStringKeySafeLoader = _companion_env._ComposeStringKeySafeLoader
_atomic_write_text = _companion_env._atomic_write_text
_compose_environment_list_item_targets = _companion_env._compose_environment_list_item_targets
_construct_compose_string_key_mapping = _companion_env._construct_compose_string_key_mapping
_missing_optional_companion_env_secret_targets = (
    _companion_env._missing_optional_companion_env_secret_targets
)
_present_optional_companion_env_secret_refs = (
    _companion_env._present_optional_companion_env_secret_refs
)
_refresh_optional_companion_env_secrets_for_resume = (
    _companion_env._refresh_optional_companion_env_secrets_for_resume
)
_remove_compose_environment_targets = _companion_env._remove_compose_environment_targets
_represent_compose_interpolation_string = _companion_env._represent_compose_interpolation_string
_restore_compose_environment_list_refs = _companion_env._restore_compose_environment_list_refs
_restore_compose_environment_refs = _companion_env._restore_compose_environment_refs
_safe_dump_compose_payload_for_resume = _companion_env._safe_dump_compose_payload_for_resume
_safe_load_compose_payload_for_resume = _companion_env._safe_load_compose_payload_for_resume


class CompanionEnvSecretPrecheckError(ComposeOperationError):
    """Raised when monitor resume cannot satisfy required companion env secrets."""

    def __init__(self, *, stderr: str, reason_code: str) -> None:
        self.operation = "companion_env_secret_precheck"
        self.returncode = 1
        self.stdout = ""
        self.stderr = stderr
        self.reason_code = reason_code
        Exception.__init__(
            self,
            "companion env secret precheck failed "
            f"(reason={reason_code}): {stderr.strip() or '<no output>'}",
        )


async def resume_pr_monitor(self: Any, workspace_id: str) -> None:
    """Resume the PR monitor for a workspace already in ``monitoring_pr``.

    The current monitor claim owner (``monitor_claimed_by`` on the row this worker
    reclaimed) is threaded into the runner so the protected-scope pause CAS can
    fence on it and refuse to clobber the row once a newer worker has reclaimed an
    expired monitor lease: a stale runner captures the owner string it set, and if
    a takeover changed ``monitor_claimed_by`` the pause CAS matches 0 rows and does
    not enter ``blocked`` with its stale preserved commit (PRRT_kwDOSJAM6s6KHtX5).
    """

    ws = await self._load_workspace(workspace_id)
    if ws is None:
        _log.warning("executor.resume_skip_unknown", workspace_id=workspace_id)
        return
    monitor_owner_id = ws.monitor_claimed_by
    if ws.status != WorkspaceStatus.monitoring_pr.value:
        _log.info(
            "executor.resume_skip_not_monitoring_pr",
            workspace_id=workspace_id,
            status=ws.status,
        )
        return
    if not await self._recheck_status(
        workspace_id,
        expected=WorkspaceStatus.monitoring_pr,
        action="resume_pr_monitor",
    ):
        return

    if await self._reject_unsupported_task_kind(
        workspace_id=workspace_id,
        workspace=ws,
        from_status=WorkspaceStatus.monitoring_pr,
    ):
        return

    if not ws.remote_push_branch and ws.task_kind == "feature_branch_pr" and ws.branch_name:
        recovered_remote_push_branch = await self._recover_feature_branch_remote_push_branch(
            workspace_id=workspace_id,
            remote_push_branch=ws.branch_name,
        )
        if recovered_remote_push_branch:
            ws.remote_push_branch = recovered_remote_push_branch

    missing = _missing_monitor_recovery_metadata(ws)
    if missing:
        await self._mark_failed(
            workspace_id=workspace_id,
            from_status=WorkspaceStatus.monitoring_pr,
            failure_reason=FailureReason.infrastructure_failure,
            message=(
                "monitor recovery: missing required persisted metadata: " + ", ".join(missing)
            )[:2000],
            reason_code="MONITOR_RECOVERY_METADATA_MISSING",
        )
        return

    compose_project = ws.compose_project_name
    compose_file_path = ws.compose_file_path
    assert compose_project is not None
    assert compose_file_path is not None
    profile = None
    companion_specs: tuple[WorkspaceCompanionSpec, ...] = ()
    companion_specs_resolved = False
    compose_up_timeout_seconds = 300
    try:
        companion_specs = companion_specs_from_task_policy(ws.task_policy)
        companion_specs_resolved = True
    except Exception:
        _log.exception(
            "executor.resume_companion_spec_resolution_failed",
            workspace_id=workspace_id,
        )

    try:
        resolved_profile = _profile_for_workspace(
            ws,
            worktree_path=self._config.worktrees_root / workspace_id,
            planning_max_iterations_default=self._config.planning_max_iterations_default,
        )
        profile = await _sync_resolved_profile(
            self,
            ws=ws,
            workspace_id=workspace_id,
            profile=resolved_profile,
            planning_max_iterations_default=self._config.planning_max_iterations_default,
        )
        # Keep the profile timeout as the fallback if stored companion policy
        # cannot be parsed during monitor recovery.
        compose_up_timeout_seconds = profile.docker.startup_timeout_seconds
        if companion_specs_resolved:
            compose_up_timeout_seconds = effective_compose_up_timeout_seconds(
                profile=profile,
                companions=companion_specs,
            )
    except Exception:
        _log.exception(
            "executor.resume_compose_timeout_resolution_failed",
            workspace_id=workspace_id,
        )

    if not await self._recheck_status(
        workspace_id,
        expected=WorkspaceStatus.monitoring_pr,
        action="resume_compose",
    ):
        return

    try:
        _precheck_required_companion_env_secrets_for_resume(
            companion_specs=companion_specs,
            environ=os.environ,
        )
        _refresh_optional_companion_env_secrets_for_resume(
            workspace_id=workspace_id,
            compose_file=Path(compose_file_path),
            companion_specs=companion_specs,
            environ=os.environ,
        )
        await self._compose.ensure_project_up(
            project_name=compose_project,
            compose_file=Path(compose_file_path),
            workspace_id=workspace_id,
            wait=True,
            compose_up_timeout_seconds=compose_up_timeout_seconds,
            force_recreate=True,
        )
    except CompanionEnvSecretPrecheckError as exc:
        _log.error(
            "executor.resume_companion_env_secret_precheck_failed",
            workspace_id=workspace_id,
            reason_code=exc.reason_code,
            stderr=exc.stderr[:1000],
        )
        await self._record_monitor_runtime_restart_failed(
            workspace_id=workspace_id,
            compose_project=compose_project,
            compose_file_path=compose_file_path,
            error=exc,
            event_reason_code="MONITOR_RECOVERY_PRECHECK_FAILED",
        )
        return
    except ComposeOperationError as exc:
        _log.error(
            "executor.resume_compose_up_failed",
            workspace_id=workspace_id,
            reason_code=exc.reason_code,
            stderr=exc.stderr[:1000],
        )
        await self._record_monitor_runtime_restart_failed(
            workspace_id=workspace_id,
            compose_project=compose_project,
            compose_file_path=compose_file_path,
            error=exc,
        )
        # Compose restart failure is not terminal for monitor recovery: the
        # prior project may still be live, and the monitor loop can still
        # reconcile PR state or surface a terminal runtime failure.

    monitor: _MonitorRunnerProto | None = self._pr_monitor
    try:
        if monitor is None and self._pr_monitor_factory is not None:
            agent = AgentRuntime(ws.agent)
            defaults = self._defaults_for(agent)
            adapter_defaults = _agent_defaults_for_workspace(ws, defaults)
            adapter = get_adapter(
                agent,
                runner=self._runner,
                defaults=adapter_defaults,
                log_store=self._log_store,
                agent_wall_timeout_seconds=self._config.agent_wall_timeout_seconds,
                agent_idle_timeout_seconds=self._config.agent_idle_timeout_seconds,
                usage_sampler=self._usage_sampler,
            )
            if profile is None:
                profile = _profile_for_workspace(
                    ws,
                    worktree_path=self._config.worktrees_root / workspace_id,
                    planning_max_iterations_default=self._config.planning_max_iterations_default,
                )
                profile = await _sync_resolved_profile(
                    self,
                    ws=ws,
                    workspace_id=workspace_id,
                    profile=profile,
                    planning_max_iterations_default=self._config.planning_max_iterations_default,
                )
            monitor = _call_pr_monitor_factory(
                self._pr_monitor_factory,
                adapter=adapter,
                profile=profile,
                workspace=ws,
                provider_recovery_default_model=(
                    _provider_recovery_default_model_for_monitor_handoff(
                        adapter=adapter,
                        defaults=defaults,
                    )
                ),
            )
    except ForgeNotSupportedError as exc:
        # This resume path bypasses the execute-time forge gate (execution_flow
        # only runs it for ready -> execution), so an unsupported forge (e.g.
        # Bitbucket) first surfaces here when the factory builds its forge
        # client. Preserve the stable FORGE_NOT_SUPPORTED reason code and its
        # doctor guidance instead of flattening it into MONITOR_RECOVERY_FAILED.
        _log.exception(
            "executor.pr_monitor_resume_forge_not_supported",
            workspace_id=workspace_id,
            reason_code=exc.reason_code,
        )
        await self._mark_failed(
            workspace_id=workspace_id,
            from_status=WorkspaceStatus.monitoring_pr,
            failure_reason=FailureReason.infrastructure_failure,
            message=f"monitor recovery: {exc.message}"[:2000],
            reason_code=exc.reason_code,
        )
        return
    except BitbucketClientError as exc:
        # The factory builds its forge client via ``make_forge_client``, so a
        # Bitbucket workspace missing BITBUCKET_API_TOKEN/BITBUCKET_EMAIL raises
        # ``BitbucketClientError`` (e.g. reason_code BITBUCKET_AUTH_NOT_CONFIGURED)
        # from ``BitbucketClient.from_env()`` before the monitor loop exists. This
        # resume path bypasses the execute-time forge gate (execution_flow only
        # runs it for ready -> execution), so the auth failure first surfaces here.
        # Preserve that actionable reason code instead of flattening it into the
        # generic MONITOR_RECOVERY_FAILED below — mirrors the initial handoff
        # path's BitbucketClientError catch so operators get the auth-setup
        # message on every monitor path.
        _log.exception(
            "executor.pr_monitor_resume_bitbucket_auth_failed",
            workspace_id=workspace_id,
            reason_code=exc.reason_code,
        )
        await self._mark_failed(
            workspace_id=workspace_id,
            from_status=WorkspaceStatus.monitoring_pr,
            failure_reason=FailureReason.infrastructure_failure,
            message=f"monitor recovery: {redact_audit_text(repr(exc), limit=1900)}"[:2000],
            reason_code=exc.reason_code,
        )
        return
    except Exception as exc:
        _log.exception("executor.pr_monitor_resume_build_failed", workspace_id=workspace_id)
        await self._mark_failed(
            workspace_id=workspace_id,
            from_status=WorkspaceStatus.monitoring_pr,
            failure_reason=FailureReason.infrastructure_failure,
            message=f"monitor recovery: failed to build PR monitor: {exc!r}"[:2000],
            reason_code="MONITOR_RECOVERY_FAILED",
        )
        return

    if monitor is None:
        await self._mark_failed(
            workspace_id=workspace_id,
            from_status=WorkspaceStatus.monitoring_pr,
            failure_reason=FailureReason.infrastructure_failure,
            message="monitor recovery: no PR monitor configured",
            reason_code="MONITOR_RECOVERY_FAILED",
        )
        return

    _log.info(
        "executor.resume_pr_monitor",
        workspace_id=workspace_id,
        pr_url=ws.pr_url,
        pr_number=ws.pr_number,
    )
    if not await self._recheck_status(
        workspace_id,
        expected=WorkspaceStatus.monitoring_pr,
        action="resume_monitor_run",
    ):
        return
    # Only forward ``monitor_owner_id`` when known so legacy/test monitor stubs
    # whose ``run`` predates the parameter keep working; the inline initial
    # handoff (under the execution claim, not a monitor claim) passes nothing.
    run_kwargs: dict[str, Any] = {}
    if monitor_owner_id is not None:
        run_kwargs["monitor_owner_id"] = monitor_owner_id
    await monitor.run(
        workspace_id=workspace_id,
        compose_project=compose_project,
        compose_file=Path(compose_file_path),
        **run_kwargs,
    )


def _precheck_required_companion_env_secrets_for_resume(
    *,
    companion_specs: tuple[WorkspaceCompanionSpec, ...],
    environ: Mapping[str, str],
) -> None:
    """Fail monitor resume early when a required env-backed companion secret is unavailable."""
    failures: list[tuple[str, str]] = []
    for spec in companion_specs:
        for secret in spec.environment_secrets:
            if not secret.required or secret.provider != "env" or secret.kind != "env":
                continue
            source_is_set = secret.value_from in environ
            source_is_empty = source_is_set and environ[secret.value_from] == ""
            if source_is_set and not source_is_empty:
                continue
            reason_code = (
                COMPANION_ENV_SECRET_SOURCE_EMPTY
                if source_is_empty
                else COMPANION_ENV_SECRET_SOURCE_MISSING
            )
            failures.append(
                (
                    reason_code,
                    f"{reason_code}: companion={spec.name}, target={secret.target}, "
                    f"provider={secret.provider}, source={secret.value_from}",
                )
            )
    if failures:
        raise CompanionEnvSecretPrecheckError(
            stderr="\n".join(stderr for _reason_code, stderr in failures),
            reason_code=failures[0][0],
        )


async def _reject_unsupported_task_kind(
    self: Any,
    *,
    workspace_id: str,
    workspace: Workspace,
    from_status: WorkspaceStatus = WorkspaceStatus.running,
) -> bool:
    """Fail fast deprecated/unknown task kinds; return True if rejected.

    Runs unconditionally — independent of any active provider recovery — so
    a deprecated ``monitor_release_pr`` or unrecognized kind can never fall
    through to the coding-agent path or resume recovery validation as
    feature work. ``feature_branch_pr`` and the ``sync_feature_pr`` /
    ``sync_release_pr`` monitors are intentionally left untouched here
    (returns False) so their recovery resumption stays intact; the sync
    handoffs are routed later by :meth:`_dispatch_non_feature_task_kind`.

    Shared by both entrypoints so the policy can't drift: :meth:`execute`
    rejects from ``running`` and :meth:`resume_pr_monitor` rejects a
    persisted legacy row from ``monitoring_pr``. ``from_status`` is the
    caller's current status so the failure transition matches it (a
    mismatched status is treated as a stale-action skip by
    :meth:`_mark_failed`, so passing the wrong one would silently no-op).

    When a reclaimed workspace still carries an active validate/rebase
    recovery operation (the worker-restart salvage of a stale ``running``
    claim), those pending/running rows are finalized as ``failed`` with the
    same terminal reason code *before* the workspace is failed, mirroring
    the recovery branches in :meth:`execute`; otherwise the workspace would
    go terminal while the recovery operation lingered unresolved.
    """
    task_kind = workspace.task_kind
    if task_kind == DEPRECATED_MONITOR_RELEASE_PR_TASK_KIND:
        message = (
            "task kind 'monitor_release_pr' is deprecated; monitor an existing "
            "release/manual PR via PR adoption with auto_merge=false instead."
        )
        if _get_active_recovery_payload(workspace) is not None:
            await self._finish_active_recovery_operations(
                workspace_id=workspace_id,
                status=OperationStatus.failed,
                reason_code=_DEPRECATED_TASK_KIND_REASON_CODE,
                error_message=message,
            )
        await self._mark_failed(
            workspace_id=workspace_id,
            from_status=from_status,
            failure_reason=FailureReason.policy_failure,
            message=message,
            reason_code=_DEPRECATED_TASK_KIND_REASON_CODE,
            details={"task_kind": task_kind},
        )
        return True
    if task_kind not in _SUPPORTED_TASK_KINDS:
        message = f"unsupported task kind {task_kind!r}; cannot run as feature work."
        if _get_active_recovery_payload(workspace) is not None:
            await self._finish_active_recovery_operations(
                workspace_id=workspace_id,
                status=OperationStatus.failed,
                reason_code=_UNSUPPORTED_TASK_KIND_REASON_CODE,
                error_message=message,
            )
        await self._mark_failed(
            workspace_id=workspace_id,
            from_status=from_status,
            failure_reason=FailureReason.policy_failure,
            message=message,
            reason_code=_UNSUPPORTED_TASK_KIND_REASON_CODE,
            details={"task_kind": task_kind},
        )
        return True
    return False


async def _gate_sync_handoff_unsupported_forge(
    self: Any,
    *,
    workspace_id: str,
    workspace: Workspace,
    worktree_path: Path,
) -> bool:
    """Re-gate the forge for a sync handoff after resolving the profile.

    The execute-time pre-resolution gate only sees ``ws.repo_url`` when the
    ``resolved_profile`` snapshot is missing, so an explicit ``forge: bitbucket``
    carried only by the requested/repo-local profile (with a GitHub or
    undetectable repo_url that detects as github) slips past it. The
    post-resolution gate in :func:`execution_flow.execute` runs only on the
    feature-agent path, which the sync dispatch returns *before* reaching.
    Resolve+persist the snapshot here so the now-concrete forge is known, then
    fail fast with ``FORGE_NOT_SUPPORTED`` — before profile setup, the
    ``sync_release_pr`` no-op completion, and the forge-client construction —
    instead of flattening a later ``ForgeNotSupportedError`` into
    ``MONITOR_UNAVAILABLE`` (``sync_feature_pr``) or silently no-op completing
    (``sync_release_pr`` with no commits ahead).

    Returns ``True`` when the workspace was failed (the caller must stop). A
    resolution failure here is deferred — the downstream handoff resolves again
    and reports it through its own error handling — so this best-effort gate
    returns ``False`` rather than masking the authoritative failure path.
    """
    try:
        profile = _profile_for_workspace(
            workspace,
            worktree_path=worktree_path,
            planning_max_iterations_default=(self._config.planning_max_iterations_default),
        )
        await _sync_resolved_profile(
            self,
            ws=workspace,
            workspace_id=workspace_id,
            profile=profile,
            planning_max_iterations_default=(self._config.planning_max_iterations_default),
        )
    except Exception:
        _log.exception(
            "executor.sync_handoff_forge_gate_resolution_failed",
            workspace_id=workspace_id,
        )
        return False
    forge_error = unsupported_forge_error(workspace)
    if forge_error is None:
        return False
    await self._mark_failed(
        workspace_id=workspace_id,
        from_status=WorkspaceStatus.running,
        failure_reason=FailureReason.infrastructure_failure,
        message=forge_error.message,
        reason_code=forge_error.reason_code,
    )
    return True


async def _dispatch_non_feature_task_kind(
    self: Any,
    *,
    workspace_id: str,
    workspace: Workspace,
    compose_project: str,
    compose_file: Path,
    worktree_path: Path,
) -> bool:
    """Route sync PR task kinds to their monitors; return True if handled.

    Only invoked when no provider recovery is active. Deprecated/unknown
    kinds are already rejected by :meth:`_reject_unsupported_task_kind`, so
    by this point the kind is ``feature_branch_pr`` (returns False to
    continue to the coding-agent path) or a ``sync_*`` monitor handoff.
    """
    task_kind = workspace.task_kind
    if task_kind == "sync_feature_pr":
        await self._handoff_sync_feature_pr_monitor(
            workspace_id=workspace_id,
            workspace=workspace,
            compose_project=compose_project,
            compose_file=compose_file,
            worktree_path=worktree_path,
        )
        return True
    if task_kind == "sync_release_pr":
        await self._handoff_sync_release_pr_monitor(
            workspace_id=workspace_id,
            workspace=workspace,
            compose_project=compose_project,
            compose_file=compose_file,
            worktree_path=worktree_path,
        )
        return True
    return False


async def _prepare_handoff_pr_monitor_profile(
    self: Any,
    *,
    workspace_id: str,
    workspace: Workspace,
    worktree_path: Path,
    compose_project: str,
    compose_file: Path,
    build_failed_log_event: str,
    build_failed_message_prefix: str,
) -> Any | None:
    """Resolve and run setup for a PR monitor handoff profile."""
    try:
        profile = _profile_for_workspace(
            workspace,
            worktree_path=worktree_path,
            planning_max_iterations_default=(self._config.planning_max_iterations_default),
        )
        profile = await _sync_resolved_profile(
            self,
            ws=workspace,
            workspace_id=workspace_id,
            profile=profile,
            planning_max_iterations_default=(self._config.planning_max_iterations_default),
        )
        if not await self._run_monitor_handoff_profile_setup(
            workspace_id=workspace_id,
            profile=profile,
            compose_project=compose_project,
            compose_file=compose_file,
            worktree_path=worktree_path,
        ):
            return None
        return profile
    except _MonitorHandoffSetupFailureError as exc:
        _log.error(
            build_failed_log_event,
            workspace_id=workspace_id,
            redacted_traceback=_redacted_exception_traceback(exc),
        )
        await _mark_failed_from_monitor_handoff_setup_failure(
            self,
            workspace_id=workspace_id,
            setup_failure=exc,
        )
        return None
    except Exception as exc:
        _log.error(
            build_failed_log_event,
            workspace_id=workspace_id,
            redacted_traceback=_redacted_exception_traceback(exc),
        )
        safe_exception = redact_audit_text(repr(exc), limit=1900)
        await self._mark_failed(
            workspace_id=workspace_id,
            from_status=WorkspaceStatus.running,
            failure_reason=FailureReason.infrastructure_failure,
            message=f"{build_failed_message_prefix}{safe_exception}"[:2000],
            reason_code=_PR_ADOPTION_MONITOR_UNAVAILABLE_REASON_CODE,
        )
        return None


async def _mark_failed_from_monitor_handoff_setup_failure(
    self: Any,
    *,
    workspace_id: str,
    setup_failure: _MonitorHandoffSetupFailureError,
) -> None:
    mark_failed_kwargs: dict[str, Any] = {
        "workspace_id": workspace_id,
        "from_status": WorkspaceStatus.running,
        "failure_reason": setup_failure.failure_reason,
        "message": setup_failure.message,
        "reason_code": setup_failure.reason_code,
    }
    if setup_failure.details is not None:
        mark_failed_kwargs["details"] = setup_failure.details
    try:
        await self._mark_failed(**mark_failed_kwargs)
        return
    except Exception:
        _log.exception(
            "executor.monitor_handoff_setup_failure_mark_failed_failed",
            workspace_id=workspace_id,
            setup_failure_reason_code=setup_failure.reason_code,
        )
    if getattr(self, "_session_factory", None) is None:
        return
    await _persist_monitor_handoff_setup_failure_directly(
        self,
        workspace_id=workspace_id,
        setup_failure=setup_failure,
    )


async def _persist_monitor_handoff_setup_failure_directly(
    self: Any,
    *,
    workspace_id: str,
    setup_failure: _MonitorHandoffSetupFailureError,
) -> None:
    """Last-resort terminal transition when the executor failure wrapper raises."""
    session_factory = getattr(self, "_session_factory", None)
    if session_factory is None:
        return
    final_reason_code = setup_failure.reason_code or setup_failure.failure_reason.value.upper()
    safe_message = redact_audit_text(setup_failure.message, limit=2000)
    payload: dict[str, Any] = {
        "failure_reason": setup_failure.failure_reason.value,
        "reason_code": final_reason_code,
        "message": safe_message,
    }
    if setup_failure.details is not None:
        payload["details"] = cast(
            dict[str, Any],
            redact_audit_value(dict(setup_failure.details)),
        )
    try:
        async with session_factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.transition_if_current(
                workspace_id,
                from_status=WorkspaceStatus.running,
                to=WorkspaceStatus.failed,
                reason_code=final_reason_code,
                payload=payload,
            )
            if ws is None:
                await session.commit()
                return
            ws.failure_reason = setup_failure.failure_reason.value
            ws.failure_message = safe_message
            await session.commit()
    except Exception:
        _log.exception(
            "executor.monitor_handoff_setup_failure_terminal_fallback_failed",
            workspace_id=workspace_id,
            setup_failure_reason_code=setup_failure.reason_code,
        )
        raise


async def _persist_monitor_handoff_failure_directly(
    self: Any,
    *,
    workspace_id: str,
    from_status: WorkspaceStatus,
    failure_reason: FailureReason,
    message: str,
    reason_code: str,
    details: Mapping[str, Any] | None = None,
) -> None:
    """Last-resort terminal transition for handoff failures outside setup."""
    session_factory = getattr(self, "_session_factory", None)
    if session_factory is None:
        return
    safe_message = redact_audit_text(message, limit=2000)
    payload: dict[str, Any] = {
        "failure_reason": failure_reason.value,
        "reason_code": reason_code,
        "message": safe_message,
    }
    if details is not None:
        payload["details"] = cast(
            dict[str, Any],
            redact_audit_value(dict(details)),
        )
    try:
        async with session_factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.transition_if_current(
                workspace_id,
                from_status=from_status,
                to=WorkspaceStatus.failed,
                reason_code=reason_code,
                payload=payload,
            )
            if ws is None:
                await session.commit()
                return
            ws.failure_reason = failure_reason.value
            ws.failure_message = safe_message
            await session.commit()
    except Exception:
        _log.exception(
            "executor.monitor_handoff_failure_terminal_fallback_failed",
            workspace_id=workspace_id,
            reason_code=reason_code,
        )
        raise


async def _mark_monitor_unavailable_failed(
    self: Any,
    *,
    workspace_id: str,
    message: str,
    from_status: WorkspaceStatus = WorkspaceStatus.running,
) -> None:
    """Mark a no-monitor handoff failure, falling back if the wrapper raises."""
    try:
        await self._mark_failed(
            workspace_id=workspace_id,
            from_status=from_status,
            failure_reason=FailureReason.infrastructure_failure,
            message=message,
            reason_code=_PR_ADOPTION_MONITOR_UNAVAILABLE_REASON_CODE,
        )
    except Exception:
        _log.exception(
            "executor.monitor_handoff_monitor_unavailable_mark_failed_failed",
            workspace_id=workspace_id,
            reason_code=_PR_ADOPTION_MONITOR_UNAVAILABLE_REASON_CODE,
        )
        await _persist_monitor_handoff_failure_directly(
            self,
            workspace_id=workspace_id,
            from_status=from_status,
            failure_reason=FailureReason.infrastructure_failure,
            message=message,
            reason_code=_PR_ADOPTION_MONITOR_UNAVAILABLE_REASON_CODE,
        )


async def _build_handoff_pr_monitor(
    self: Any,
    *,
    workspace_id: str,
    workspace: Workspace,
    worktree_path: Path,
    compose_project: str,
    compose_file: Path,
    build_failed_log_event: str,
    build_failed_message_prefix: str,
    profile: Any | None = None,
    run_profile_setup: bool = True,
    stale_action: str = "monitor_handoff_build_pr_monitor",
) -> _MonitorRunnerProto | None:
    """Build the PR monitor for a handoff, marking the workspace failed on error.

    Shared by the ``sync_feature_pr`` and ``sync_release_pr`` handoffs. Returns
    ``None`` (after transitioning to ``failed``) when no monitor can be built.
    A caller that supplies ``profile`` is handing in an already prepared handoff
    profile and must also set ``run_profile_setup=False`` so setup is not run
    a second time.
    """
    monitor: _MonitorRunnerProto | None = self._pr_monitor
    if profile is not None and run_profile_setup:
        raise ValueError("pre-resolved handoff profiles must pass run_profile_setup=False")
    if monitor is None and self._pr_monitor_factory is None:
        await _mark_monitor_unavailable_failed(
            self,
            workspace_id=workspace_id,
            message=f"{build_failed_message_prefix}no PR monitor configured",
        )
        return None

    try:
        if profile is None:
            profile = _profile_for_workspace(
                workspace,
                worktree_path=worktree_path,
                planning_max_iterations_default=(self._config.planning_max_iterations_default),
            )
            profile = await _sync_resolved_profile(
                self,
                ws=workspace,
                workspace_id=workspace_id,
                profile=profile,
                planning_max_iterations_default=(self._config.planning_max_iterations_default),
            )
        if run_profile_setup and not await self._run_monitor_handoff_profile_setup(
            workspace_id=workspace_id,
            profile=profile,
            compose_project=compose_project,
            compose_file=compose_file,
            worktree_path=worktree_path,
        ):
            return None
        if not await self._recheck_status(
            workspace_id,
            expected=WorkspaceStatus.running,
            action=stale_action,
        ):
            return None
        if monitor is None and self._pr_monitor_factory is not None:
            agent = AgentRuntime(workspace.agent)
            defaults = self._defaults_for(agent)
            adapter_defaults = _agent_defaults_for_workspace(workspace, defaults)
            adapter = get_adapter(
                agent,
                runner=self._runner,
                defaults=adapter_defaults,
                log_store=self._log_store,
                agent_wall_timeout_seconds=self._config.agent_wall_timeout_seconds,
                agent_idle_timeout_seconds=self._config.agent_idle_timeout_seconds,
                usage_sampler=self._usage_sampler,
            )
            monitor = _call_pr_monitor_factory(
                self._pr_monitor_factory,
                adapter=adapter,
                profile=profile,
                workspace=workspace,
                provider_recovery_default_model=(
                    _provider_recovery_default_model_for_monitor_handoff(
                        adapter=adapter,
                        defaults=defaults,
                    )
                ),
            )
    except _MonitorHandoffSetupFailureError as exc:
        _log.error(
            build_failed_log_event,
            workspace_id=workspace_id,
            redacted_traceback=_redacted_exception_traceback(exc),
        )
        await _mark_failed_from_monitor_handoff_setup_failure(
            self,
            workspace_id=workspace_id,
            setup_failure=exc,
        )
        return None
    except BitbucketClientError as exc:
        # The factory builds its forge client via ``make_forge_client``, so a
        # Bitbucket workspace missing BITBUCKET_API_TOKEN/BITBUCKET_EMAIL raises
        # ``BitbucketClientError`` (e.g. reason_code BITBUCKET_AUTH_NOT_CONFIGURED)
        # from ``BitbucketClient.from_env()`` before the monitor loop exists.
        # Preserve that actionable reason code instead of flattening it into the
        # generic MONITOR_UNAVAILABLE failure below — mirrors the release-sync
        # handoff's BitbucketClientError catch so operators get the auth-setup
        # message on every handoff path.
        _log.error(
            build_failed_log_event,
            workspace_id=workspace_id,
            reason_code=exc.reason_code,
            redacted_traceback=_redacted_exception_traceback(exc),
        )
        safe_exception = redact_audit_text(repr(exc), limit=1900)
        await self._mark_failed(
            workspace_id=workspace_id,
            from_status=WorkspaceStatus.running,
            failure_reason=FailureReason.infrastructure_failure,
            message=f"{build_failed_message_prefix}{safe_exception}"[:2000],
            reason_code=exc.reason_code,
        )
        return None
    except Exception as exc:
        _log.error(
            build_failed_log_event,
            workspace_id=workspace_id,
            redacted_traceback=_redacted_exception_traceback(exc),
        )
        safe_exception = redact_audit_text(repr(exc), limit=1900)
        await self._mark_failed(
            workspace_id=workspace_id,
            from_status=WorkspaceStatus.running,
            failure_reason=FailureReason.infrastructure_failure,
            message=f"{build_failed_message_prefix}{safe_exception}"[:2000],
            reason_code=_PR_ADOPTION_MONITOR_UNAVAILABLE_REASON_CODE,
        )
        return None

    if monitor is None:
        await _mark_monitor_unavailable_failed(
            self,
            workspace_id=workspace_id,
            message=f"{build_failed_message_prefix}no PR monitor configured",
        )
        return None

    # Monitor-only handoffs (``sync_feature_pr`` / ``sync_release_pr``, including
    # adopted PRs that persist ``sync_feature_pr``) reach the PR monitor without
    # ever running ``execute``'s pre-agent Ollama auto-pull step: that block sits
    # after ``_dispatch_non_feature_task_kind`` returns. Create-time readiness can
    # admit an absent local model as ``OLLAMA_MODEL_PULL_PENDING`` (deferring the
    # pull to that step), so for an OpenCode/Ollama workspace the requested model
    # may still be missing here. The monitor's first PR repair invokes the agent
    # CLI, so ensure/pull the model now — while still ``running`` — instead of
    # letting OpenCode fail later as an opaque ``AGENT_CLI_FAILED``. No-op for
    # non-OpenCode runtimes and for a sidecar daemon unreachable from the worker.
    if not await self._ensure_ollama_model_or_mark_failed(
        workspace_id=workspace_id,
        ws=workspace,
    ):
        return None
    return monitor


async def _record_monitor_runtime_restart_failed(
    self: Any,
    *,
    workspace_id: str,
    compose_project: str,
    compose_file_path: str,
    error: ComposeOperationError,
    event_reason_code: str = "MONITOR_RECOVERY_COMPOSE_FAILED",
) -> None:
    try:
        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(workspace_id)
            if ws is None or ws.status != WorkspaceStatus.monitoring_pr.value:
                return
            await repo.add_event(
                ws,
                event_type="workspace.monitor_runtime_restart_failed",
                reason_code=event_reason_code,
                payload={
                    "compose_project_name": compose_project,
                    "compose_file_path": compose_file_path,
                    "operation": error.operation,
                    "returncode": error.returncode,
                    "stderr": error.stderr[:1000],
                    "reason_code": error.reason_code,
                },
            )
            await session.commit()
    except Exception:
        _log.exception(
            "executor.monitor_runtime_restart_failed_record_failed",
            workspace_id=workspace_id,
            compose_project_name=compose_project,
            compose_file_path=compose_file_path,
            reason_code=error.reason_code,
        )
