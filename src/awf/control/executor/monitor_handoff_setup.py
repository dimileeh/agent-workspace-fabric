"""Profile setup helpers for PR monitor handoff workspaces."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from awf.common.audit import redact_audit_text
from awf.common.compose_exec import ComposeExecCleanupError, cleanup_failure_message
from awf.common.logging import get_logger
from awf.common.redaction import redact_secrets
from awf.control.executor.constants import (
    PR_MONITOR_SETUP_FAILED_REASON_CODE,
    PROFILE_VALIDATE_TOOLCHAIN_UNPROVISIONED_REASON_CODE,
)
from awf.control.executor.helpers import _failure_reason_for_phase
from awf.control.executor.logging_ops import (
    SETUP_DEPENDENCY_NETWORK_FAILURE,
    _setup_dependency_network_failure_details,
)
from awf.control.executor.mirror_hooks_repair import repair_mirror_hooks_path_or_mark_failed
from awf.db.enums import FailureReason, WorkspaceStatus
from awf.node.git_manager import mirror_path_for_worktree, repair_mirror_hooks_path
from awf.runtime.ownership import (
    AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE,
    EXECUTOR_AGENT_RUNTIME_OWNERSHIP_REPAIR_EVENT_NAME,
    repair_agent_runtime_ownership,
)

_log = get_logger(__name__)


class _MonitorHandoffSetupFailureError(RuntimeError):
    """Setup failure payload that still needs a terminal transition."""

    def __init__(
        self,
        *,
        failure_reason: FailureReason,
        message: str,
        reason_code: str | None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.failure_reason = failure_reason
        self.message = message
        self.reason_code = reason_code
        self.details = dict(details) if details is not None else None


async def _mark_failed_or_raise_setup_failure(
    self: Any,
    *,
    workspace_id: str,
    failure_reason: FailureReason,
    message: str,
    reason_code: str | None,
    details: dict[str, Any] | None = None,
) -> None:
    mark_failed_kwargs: dict[str, Any] = {
        "workspace_id": workspace_id,
        "from_status": WorkspaceStatus.running,
        "failure_reason": failure_reason,
        "message": message,
        "reason_code": reason_code,
    }
    if details is not None:
        mark_failed_kwargs["details"] = details
    try:
        await self._mark_failed(**mark_failed_kwargs)
    except Exception as exc:
        raise _MonitorHandoffSetupFailureError(
            failure_reason=failure_reason,
            message=message,
            reason_code=reason_code,
            details=details,
        ) from exc


async def _run_monitor_handoff_profile_preflight(
    self: Any,
    *,
    workspace_id: str,
    validation: Any,
    profile: Any,
) -> bool:
    profile_preflight = getattr(validation, "run_profile_tool_preflight", None)
    if not callable(profile_preflight):
        return True
    try:
        profile_preflight_result = await profile_preflight(
            workspace_id=workspace_id,
            profile=profile,
        )
    except _MonitorHandoffSetupFailureError:
        raise
    except Exception as exc:
        _log.exception(
            "executor.monitor_handoff_profile_preflight_failed",
            workspace_id=workspace_id,
        )
        safe_error = redact_audit_text(repr(exc))
        await _mark_failed_or_raise_setup_failure(
            self,
            workspace_id=workspace_id,
            failure_reason=FailureReason.infrastructure_failure,
            message=f"monitor handoff profile preflight failed: {safe_error}"[:2000],
            reason_code=PR_MONITOR_SETUP_FAILED_REASON_CODE,
        )
        return False

    if profile_preflight_result.all_passed:
        return True

    first_fail = profile_preflight_result.first_failure
    failure_message = (
        f"profile preflight failed: {redact_audit_text(first_fail.command)}"
        if first_fail is not None
        else "profile preflight failed"
    )[:2000]
    await _mark_failed_or_raise_setup_failure(
        self,
        workspace_id=workspace_id,
        failure_reason=_failure_reason_for_phase(first_fail),
        message=failure_message,
        reason_code=(getattr(first_fail, "reason_code", None) if first_fail is not None else None),
    )
    return False


async def _run_monitor_handoff_validate_toolchain_probe(
    self: Any,
    *,
    workspace_id: str,
    validation: Any,
    compose_project: str,
    compose_file: Path,
    profile: Any,
) -> bool:
    """Fail the adopt-pr handoff early when a ``validate`` tool is unprovisioned.

    Adopt-pr skips the coding agent (``PR_ADOPTION_SKIP_AGENT``), so the profile
    ``setup`` phase is the only provisioning step. After a green setup, probe that
    each ``validate``-phase command's executable resolves on PATH in the
    workspace container. If one or more is missing, mark the workspace ``failed``
    here with ``PROFILE_VALIDATE_TOOLCHAIN_UNPROVISIONED`` — naming the tool(s)
    and pointing at ``setup`` — instead of transitioning to ``monitoring_pr`` and
    dying ``127`` later at ``sync_base_push``. Returns ``True`` to proceed.

    Strictly scoped to the agent-skipped monitor handoff: the probe runs only
    here, never on the create path (which provisions via the agent run). Legacy
    ``_validation`` stubs without the probe method are a no-op. A probe-infra
    failure (compose-exec error / timeout) is logged and treated as non-blocking
    so it is never mis-reported as a profile gap.
    """
    probe = getattr(validation, "probe_validate_command_tools", None)
    if not callable(probe):
        return True
    try:
        result = await probe(
            workspace_id=workspace_id,
            compose_project=compose_project,
            compose_file=compose_file,
            profile=profile,
        )
    except Exception as exc:
        # Non-blocking probe-infra failure: preserve the structured reason_code
        # and a redacted error, then proceed — never fail the adoption on a probe
        # defect, and never leak a raw traceback carrying compose-exec stderr.
        _log.warning(
            "executor.monitor_handoff_validate_toolchain_probe_failed",
            workspace_id=workspace_id,
            reason_code=getattr(exc, "reason_code", None),
            error=redact_secrets(str(exc))[:1000],
        )
        return True

    if result.probe_errored:
        # The probe could not run cleanly (unreachable container / timeout); that
        # is not evidence of a profile gap, so proceed rather than fail.
        _log.warning(
            "executor.monitor_handoff_validate_toolchain_probe_errored",
            workspace_id=workspace_id,
        )
        return True

    if not result.missing:
        return True

    missing_tools = sorted({redact_audit_text(target.tool) for target in result.missing})
    representative = result.missing[0]
    safe_command = redact_audit_text(representative.command)
    safe_tool = redact_audit_text(representative.tool)
    message = (
        f"validate command `{safe_command}` needs `{safe_tool}`, which is not on "
        "PATH after setup. The adopt-pr path skips the coding agent, so the "
        "profile's setup phase must install everything validate needs (e.g. add "
        "the install step for the missing tool to .awf/workspace.yml setup). "
        f"Missing tools: {', '.join(missing_tools)}."
    )[:2000]
    # A validate tool the profile's setup never installed is a profile gap, not
    # transient infrastructure: retrying the same .awf/workspace.yml cannot fix
    # it. Use ``profile_resolution_failure`` (matching the analogous
    # ``PROFILE_VALIDATION_TOOL_UNAVAILABLE`` classification) so failure analysis
    # surfaces a profile fix instead of recommending a retry, and the scheduler
    # withholds the infrastructure-failure retry bonus.
    await _mark_failed_or_raise_setup_failure(
        self,
        workspace_id=workspace_id,
        failure_reason=FailureReason.profile_resolution_failure,
        message=message,
        reason_code=PROFILE_VALIDATE_TOOLCHAIN_UNPROVISIONED_REASON_CODE,
        details={"missing_tools": missing_tools},
    )
    return False


async def _run_monitor_handoff_profile_setup(
    self: Any,
    *,
    workspace_id: str,
    profile: Any,
    compose_project: str,
    compose_file: Path,
    worktree_path: Path,
) -> bool:
    """Run profile setup before handing an adopted/release PR to the monitor."""
    mirror_path = mirror_path_for_worktree(worktree_path)

    async def _repair_mirror_hooks_path_or_mark_failed(*, failure_stage: str) -> bool:
        return await repair_mirror_hooks_path_or_mark_failed(
            executor=self,
            workspace_id=workspace_id,
            mirror_path=mirror_path,
            repair_mirror_hooks_path_fn=repair_mirror_hooks_path,
            recovery_active=False,
            failure_stage=failure_stage,
        )

    try:
        validation = getattr(self, "_validation", None)
        if validation is None:
            await _mark_failed_or_raise_setup_failure(
                self,
                workspace_id=workspace_id,
                failure_reason=FailureReason.infrastructure_failure,
                message="monitor handoff profile setup failed: no validation runner configured",
                reason_code=PR_MONITOR_SETUP_FAILED_REASON_CODE,
            )
            return False
        if not await repair_agent_runtime_ownership(
            logger=_log,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            reason="profile_setup",
            event_name=EXECUTOR_AGENT_RUNTIME_OWNERSHIP_REPAIR_EVENT_NAME,
            reason_code=AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE,
        ):
            await _mark_failed_or_raise_setup_failure(
                self,
                workspace_id=workspace_id,
                failure_reason=FailureReason.infrastructure_failure,
                message="agent runtime ownership repair failed before profile setup",
                reason_code=AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE,
            )
            return False
        if not await _repair_mirror_hooks_path_or_mark_failed(
            failure_stage="before monitor handoff setup"
        ):
            return False
        setup_result = await validation.run_profile_phases(
            workspace_id=workspace_id,
            compose_project=compose_project,
            compose_file=compose_file,
            profile=profile,
            phase_names=("setup", "pre_agent"),
            worktree_path=worktree_path,
        )
    except _MonitorHandoffSetupFailureError:
        raise
    except ComposeExecCleanupError as exc:
        if not await _repair_mirror_hooks_path_or_mark_failed(
            failure_stage="after monitor handoff setup cleanup failure"
        ):
            return False
        await _mark_failed_or_raise_setup_failure(
            self,
            workspace_id=workspace_id,
            failure_reason=FailureReason.infrastructure_failure,
            message=cleanup_failure_message(exc),
            reason_code=exc.reason_code,
        )
        return False
    except Exception as exc:
        _log.exception("executor.monitor_handoff_setup_failed", workspace_id=workspace_id)
        safe_error = redact_audit_text(repr(exc))
        await _mark_failed_or_raise_setup_failure(
            self,
            workspace_id=workspace_id,
            failure_reason=FailureReason.infrastructure_failure,
            message=f"monitor handoff profile setup failed: {safe_error}"[:2000],
            reason_code=PR_MONITOR_SETUP_FAILED_REASON_CODE,
        )
        return False

    try:
        await self._record_setup_dependency_network_events(
            workspace_id=workspace_id,
            result=setup_result,
        )
    except Exception:
        _log.exception(
            "executor.monitor_handoff_setup_dependency_network_event_record_failed",
            workspace_id=workspace_id,
            setup_all_passed=setup_result.all_passed,
        )

    if setup_result.all_passed:
        if not await _repair_mirror_hooks_path_or_mark_failed(
            failure_stage="after successful monitor handoff setup"
        ):
            return False
        # Mirror the ``execute`` path: probe the container for declared
        # toolchains and record any ``RUNTIME_TOOLCHAIN_UNAVAILABLE`` warnings
        # after a green setup. The probe is strictly additive and non-blocking,
        # so a recorder error is swallowed and never affects the handoff — but
        # omitting it entirely would let adopted/release-PR workspaces silently
        # miss the toolchain-availability warnings their executed peers get.
        try:
            await self._record_runtime_toolchain_findings(
                workspace_id=workspace_id,
                compose_project=compose_project,
                compose_file=compose_file,
                profile=profile,
            )
        except Exception as exc:
            # Non-blocking probe/recorder failure: preserve the structured
            # reason_code and a redacted error instead of a raw traceback that
            # could carry compose-exec stderr secrets.
            _log.warning(
                "executor.monitor_handoff_runtime_toolchain_probe_record_failed",
                workspace_id=workspace_id,
                reason_code=getattr(exc, "reason_code", None),
                error=redact_secrets(str(exc))[:1000],
            )
        # Adopt-pr skips the agent, so the only thing that provisions the validate
        # toolchain is the setup phase just run. Fail early + clearly if a validate
        # tool is still missing, before transitioning to monitoring_pr.
        if not await _run_monitor_handoff_validate_toolchain_probe(
            self,
            workspace_id=workspace_id,
            validation=validation,
            compose_project=compose_project,
            compose_file=compose_file,
            profile=profile,
        ):
            return False
        return await _run_monitor_handoff_profile_preflight(
            self,
            workspace_id=workspace_id,
            validation=validation,
            profile=profile,
        )

    first_fail = setup_result.first_failure
    if not await _repair_mirror_hooks_path_or_mark_failed(
        failure_stage="after monitor handoff setup failure"
    ):
        return False
    setup_dependency_details = _setup_dependency_network_failure_details(first_fail)
    setup_failure_reason_code = (
        SETUP_DEPENDENCY_NETWORK_FAILURE
        if setup_dependency_details is not None
        else PR_MONITOR_SETUP_FAILED_REASON_CODE
    )
    failure_reason = _failure_reason_for_phase(first_fail)
    failure_message = (
        f"profile setup failed: {redact_audit_text(first_fail.command)}"
        if first_fail is not None
        else "profile setup failed"
    )[:2000]
    try:
        await _mark_failed_or_raise_setup_failure(
            self,
            workspace_id=workspace_id,
            failure_reason=failure_reason,
            message=failure_message,
            reason_code=setup_failure_reason_code,
            details=setup_dependency_details,
        )
    except _MonitorHandoffSetupFailureError as setup_failure:
        _log.exception(
            "executor.monitor_handoff_setup_mark_failed_after_command_failure_failed",
            workspace_id=workspace_id,
            setup_failure_reason_code=setup_failure.reason_code,
        )
        raise
    return False
