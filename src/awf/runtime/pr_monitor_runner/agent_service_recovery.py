"""Agent compose-service recovery for PR monitor agent runs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, NoReturn, cast

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from awf.adapters.base import AgentRunError, AgentRunResult
from awf.adapters.provider_failures import (
    AGENT_IDLE_TIMEOUT,
    AGENT_SERVICE_UNHEALTHY,
    AGENT_TIMEOUT,
    classify_provider_failure,
)
from awf.adapters.runtime_executor import AgentRuntimeGitPreparation
from awf.common.command_evidence import append_command_evidence
from awf.common.commands import CommandResult
from awf.common.compose_exec import EXEC_PROCESS_CLEANUP_FAILED, ComposeExecCleanupError
from awf.common.git_identity import git_safe_directory_config_args
from awf.control.protected_file_diffs import protected_file_diffs_for_committed_paths
from awf.control.quality_gates import (
    QualityGateViolation,
    find_protected_quality_gate_changes,
    quality_gate_violation_message,
)
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.node.companion_services import companion_specs_from_task_policy
from awf.node.compose_manager import ComposeManager, ComposeOperationError
from awf.node.git_manager import (
    GitOperationError,
    git_env_without_object_lookup_overrides,
    mirror_path_for_worktree,
    repair_mirror_hooks_path,
    verify_head_object_exists,
)
from awf.node.stack_launcher import effective_compose_up_timeout_seconds
from awf.profiles.models import WorkspaceProfile
from awf.runtime.hosted_pr_identity import (
    _nonblank_str,
    hosted_pr_identity_for_workspace,
)
from awf.runtime.inspection import RuntimeInspector, probe_agent_service_health
from awf.runtime.ownership import (
    MONITOR_AGENT_RUNTIME_OWNERSHIP_REPAIR_EVENT_NAME,
    repair_agent_runtime_ownership,
)
from awf.runtime.pr_monitor_runner.constants import (
    _HEAD_OBJECT_MISSING_RECOVERED_REASON,
    _HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON,
    _MIRROR_HOOKS_PATH_POISONED_REASON,
    _PROTECTED_SCOPE_DIFF_UNAVAILABLE_REASON,
    _PROTECTED_SCOPE_PUSH_BLOCKED_REASON,
)
from awf.runtime.pr_monitor_runner.logging import _log
from awf.runtime.pr_monitor_runner.mirror_hooks import mirror_hooks_repair_failure_details
from awf.runtime.pr_monitor_runner.path_parsing import _changed_paths_from_name_status_z
from awf.runtime.pr_monitor_runner.remote_repair import (
    _recover_missing_head_object_from_filesystem,
)
from awf.runtime.pr_monitor_runner.types import (
    ProtectedScopeDiffError,
    ProviderRecoveryRetryError,
    _MonitorAgentRuntimeOwnershipRepairFailedError,
    _MonitorAgentServiceRecoveryFailedError,
    _MonitorAgentServiceRecoverySupersededError,
    _MonitorHeadObjectMissingError,
    _MonitorMirrorHooksPathRepairFailedError,
    _MonitorPolicyBlockedError,
)

_AGENT_SERVICE_TIMEOUT_REASON_CODES = frozenset({AGENT_IDLE_TIMEOUT, AGENT_TIMEOUT})
_AGENT_SERVICE_RESTART_ATTEMPTS = 2
_MONITOR_AGENT_SERVICE_RESTART_TIMEOUT_SECONDS = 300
_HOSTED_REMOTE_HEAD_DELTA_UNAVAILABLE_REASON = "HOSTED_REMOTE_HEAD_DELTA_UNAVAILABLE"
_HOSTED_GIT_PREPARATION_BASE_REF_MISMATCH_REASON = "HOSTED_GIT_PREPARATION_BASE_REF_MISMATCH"


async def _run_monitor_agent_with_service_recovery(
    self: Any,
    *,
    workspace_id: str,
    compose_project: str,
    compose_file: Path,
    prompt: str,
    log_source: str,
    command_evidence: list[str] | None = None,
    operation_start_head: str | None = None,
    state: Any | None = None,
    git_preparation: AgentRuntimeGitPreparation | None = None,
) -> AgentRunResult:
    """Run the monitor agent while recovering from agent-service failures."""
    hosted_pr_identity = (
        await _hosted_pr_identity_for_workspace(self, workspace_id, state=state)
        if self._deps.adapter.is_hosted
        else None
    )
    restart_attempts = 0
    while True:
        if self._deps.adapter.is_hosted and git_preparation is not None:
            trusted_base_ref = _nonblank_str((hosted_pr_identity or {}).get("base_ref"))
            if trusted_base_ref != git_preparation.base_ref:
                raise AgentRunError(
                    agent=self._deps.adapter.name,
                    result=CommandResult(
                        returncode=1,
                        stdout="",
                        stderr=(
                            "hosted git preparation base ref does not match "
                            "the trusted PR base identity"
                        ),
                    ),
                    reason_code=_HOSTED_GIT_PREPARATION_BASE_REF_MISMATCH_REASON,
                    details={
                        "preparation_base_ref": git_preparation.base_ref,
                        "trusted_base_ref": trusted_base_ref,
                    },
                )
        try:
            if self._deps.adapter.is_hosted:
                hosted_run_kwargs: dict[str, Any] = {
                    "compose_project": compose_project,
                    "compose_file": compose_file,
                    "prompt": prompt,
                    "workspace_id": workspace_id,
                    "log_source": log_source,
                    "hosted_pr_identity": hosted_pr_identity,
                    "profile": getattr(self, "_workspace_profile", None),
                    "worktree_path": self._worktrees_root / workspace_id,
                }
                if git_preparation is not None:
                    hosted_run_kwargs["git_preparation"] = git_preparation
                result = await self._deps.adapter.run(**hosted_run_kwargs)
            else:
                local_run_kwargs: dict[str, Any] = {
                    "compose_project": compose_project,
                    "compose_file": compose_file,
                    "prompt": prompt,
                    "workspace_id": workspace_id,
                    "log_source": log_source,
                }
                profile = getattr(self, "_workspace_profile", None)
                if profile is not None:
                    local_run_kwargs["profile"] = profile
                result = await self._deps.adapter.run(**local_run_kwargs)
        except AgentRunError as exc:
            if self._deps.adapter.is_hosted:
                terminal_head_sha = _nonblank_str(exc.details.get("terminal_head_sha"))
                if terminal_head_sha is not None:
                    terminal_head_evidence = list(command_evidence or ())
                    append_command_evidence(
                        terminal_head_evidence,
                        stdout=exc.result.stdout,
                        stderr=exc.result.stderr,
                    )
                    synced_head_sha = await _sync_hosted_worktree_to_terminal_head(
                        self,
                        workspace_id=workspace_id,
                        hosted_pr_identity=hosted_pr_identity,
                        terminal_head_sha=terminal_head_sha,
                        command_evidence=terminal_head_evidence,
                        operation_start_head=operation_start_head,
                    )
                    if state is not None:
                        await _record_hosted_terminal_head_sync(
                            self,
                            state,
                            synced_head_sha=synced_head_sha,
                            operation_start_head=operation_start_head,
                            worktree_path=self._worktrees_root / workspace_id,
                        )
            recovered = await _recover_monitor_agent_service_after_error(
                self,
                workspace_id=workspace_id,
                compose_project=compose_project,
                compose_file=compose_file,
                exc=exc,
                restart_attempts=restart_attempts,
                command_evidence=command_evidence,
            )
            if recovered is None:
                raise
            restart_attempts = recovered
            if self._deps.adapter.is_hosted and state is not None:
                hosted_pr_identity = await _hosted_pr_identity_for_workspace(
                    self,
                    workspace_id,
                    state=state,
                )
            await _rerun_monitor_agent_pre_launch_guards(
                self,
                workspace_id=workspace_id,
                source_reason_code=exc.reason_code,
                service_healthy=False,
                restart_attempts=restart_attempts,
            )
            continue
        except ComposeExecCleanupError as exc:
            recovered = await _recover_monitor_agent_service_after_cleanup_error(
                self,
                workspace_id=workspace_id,
                compose_project=compose_project,
                compose_file=compose_file,
                exc=exc,
                restart_attempts=restart_attempts,
                command_evidence=command_evidence,
                operation_start_head=operation_start_head,
            )
            if recovered is None:
                raise
            restart_attempts = recovered
            await _rerun_monitor_agent_pre_launch_guards(
                self,
                workspace_id=workspace_id,
                source_reason_code=exc.reason_code,
                service_healthy=False,
                restart_attempts=restart_attempts,
            )
            continue
        if self._deps.adapter.is_hosted:
            if not result.terminal_head_sha:
                raise AgentRunError(
                    agent=self._deps.adapter.name,
                    result=CommandResult(
                        returncode=1,
                        stdout=result.stdout,
                        stderr="hosted repair completed without terminal_head_sha",
                    ),
                    reason_code="HOSTED_REMOTE_HEAD_MISSING",
                )
            append_command_evidence(
                command_evidence,
                stdout=result.stdout,
                stderr=result.stderr,
            )
            synced_head_sha = await _sync_hosted_worktree_to_terminal_head(
                self,
                workspace_id=workspace_id,
                hosted_pr_identity=hosted_pr_identity,
                terminal_head_sha=result.terminal_head_sha,
                command_evidence=command_evidence or (),
                operation_start_head=operation_start_head,
            )
            if state is not None:
                await _record_hosted_terminal_head_sync(
                    self,
                    state,
                    synced_head_sha=synced_head_sha,
                    operation_start_head=operation_start_head,
                    worktree_path=self._worktrees_root / workspace_id,
                )
        else:
            append_command_evidence(
                command_evidence,
                stdout=result.stdout,
                stderr=result.stderr,
            )
        return cast(AgentRunResult, result)


async def _record_hosted_terminal_head_sync(
    self: Any,
    state: Any,
    *,
    synced_head_sha: str,
    operation_start_head: str | None,
    worktree_path: Path,
) -> None:
    """Record hosted terminal sync; mark advanced only on forward ancestry.

    SHA inequality alone accepts lateral/older force-pushes that drop a fix.
    Require ``synced_head_sha`` to descend from the item start head when the
    runner can verify ancestry; otherwise fail closed and leave the flag false.
    """
    state.last_push_sha = synced_head_sha
    start_head = _nonblank_str(operation_start_head)
    if start_head is None or synced_head_sha.lower() == start_head.lower():
        return
    descends = getattr(self, "_head_descends_from", None)
    if not callable(descends) or not worktree_path.exists():
        return
    if await descends(
        worktree_path=worktree_path,
        ancestor=start_head,
        descendant=synced_head_sha,
    ):
        state.hosted_terminal_head_advanced = True


async def _hosted_pr_identity_for_workspace(
    self: Any,
    workspace_id: str,
    *,
    state: Any | None = None,
) -> dict[str, object]:
    ws = await self._load_workspace(workspace_id)
    return hosted_pr_identity_for_workspace(ws, state=state)


async def _sync_hosted_worktree_to_terminal_head(
    self: Any,
    *,
    workspace_id: str,
    hosted_pr_identity: dict[str, object] | None,
    terminal_head_sha: str,
    command_evidence: Sequence[str] = (),
    operation_start_head: str | None = None,
) -> str:
    identity = hosted_pr_identity or {}
    repo_url = _nonblank_str(identity.get("head_repo_url")) or _nonblank_str(
        identity.get("repo_url")
    )
    head_ref = _nonblank_str(identity.get("head_ref"))
    worktree_path = self._worktrees_root / workspace_id
    if repo_url is None or head_ref is None:
        raise AgentRunError(
            agent=self._deps.adapter.name,
            result=CommandResult(
                returncode=1,
                stdout="",
                stderr="hosted repair missing remote PR head identity",
            ),
            reason_code="HOSTED_REMOTE_HEAD_IDENTITY_MISSING",
        )
    git_env = git_env_without_object_lookup_overrides()
    delta_base_sha = _nonblank_str(operation_start_head)
    if delta_base_sha is None:
        current_head = await self._deps.runner.run(
            [
                "git",
                *git_safe_directory_config_args(worktree_path),
                "-C",
                str(worktree_path),
                "rev-parse",
                "HEAD",
            ],
            env=git_env,
        )
        delta_base_sha = current_head.stdout.strip()
        if not current_head.ok or not delta_base_sha:
            raise AgentRunError(
                agent=self._deps.adapter.name,
                result=current_head,
                reason_code=_HOSTED_REMOTE_HEAD_DELTA_UNAVAILABLE_REASON,
            )
    fetch = await self._deps.runner.run(
        [
            "git",
            *git_safe_directory_config_args(worktree_path),
            "-C",
            str(worktree_path),
            "fetch",
            "--no-tags",
            repo_url,
            head_ref,
        ],
        env=git_env,
    )
    if not fetch.ok:
        raise AgentRunError(
            agent=self._deps.adapter.name,
            result=fetch,
            reason_code="HOSTED_REMOTE_HEAD_FETCH_FAILED",
        )
    rev_parse = await self._deps.runner.run(
        [
            "git",
            *git_safe_directory_config_args(worktree_path),
            "-C",
            str(worktree_path),
            "rev-parse",
            "FETCH_HEAD",
        ],
        env=git_env,
    )
    fetched_sha = str(rev_parse.stdout).strip()
    if not rev_parse.ok or fetched_sha.lower() != terminal_head_sha.lower():
        raise AgentRunError(
            agent=self._deps.adapter.name,
            result=CommandResult(
                returncode=1,
                stdout=rev_parse.stdout,
                stderr=(
                    "hosted repair terminal head mismatch: "
                    f"reported {terminal_head_sha}, fetched {fetched_sha or '<unknown>'}"
                ),
            ),
            reason_code="HOSTED_REMOTE_HEAD_MISMATCH",
        )
    reset = await self._deps.runner.run(
        [
            "git",
            *git_safe_directory_config_args(worktree_path),
            "-C",
            str(worktree_path),
            "reset",
            "--hard",
            fetched_sha,
        ],
        env=git_env,
    )
    if not reset.ok:
        raise AgentRunError(
            agent=self._deps.adapter.name,
            result=reset,
            reason_code="HOSTED_REMOTE_HEAD_SYNC_FAILED",
        )
    await _gate_hosted_terminal_head_delta(
        self,
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        base_ref=delta_base_sha,
        terminal_head_sha=fetched_sha,
        command_evidence=command_evidence,
    )
    return fetched_sha


_HOSTED_REMOTE_HEAD_ROLLBACK_FAILED_REASON = "HOSTED_REMOTE_HEAD_ROLLBACK_FAILED"


async def _rollback_hosted_terminal_head_on_remote(
    self: Any,
    *,
    workspace_id: str,
    hosted_pr_identity: dict[str, object] | None,
    rollback_target_sha: str,
    expected_remote_head_sha: str,
) -> bool:
    """Force-push a rollback target to the hosted PR head after local rewind.

    Hosted agents publish terminal commits to the PR branch before AWF syncs the
    local worktree. Local-only ``git reset --hard`` therefore leaves unaccepted
    protocol-retry edits on the remote branch; this helper rewinds the published
    head and verifies the fetch matches ``rollback_target_sha``.
    """
    identity = hosted_pr_identity or {}
    repo_url = _nonblank_str(identity.get("head_repo_url")) or _nonblank_str(
        identity.get("repo_url")
    )
    head_ref = _nonblank_str(identity.get("head_ref"))
    worktree_path = self._worktrees_root / workspace_id
    if repo_url is None or head_ref is None:
        _log.warning(
            "monitor.hosted_terminal_head_remote_rollback_skipped",
            workspace_id=workspace_id,
            reason="missing_pr_identity",
        )
        return False

    git_env = git_env_without_object_lookup_overrides()
    ref_name = f"refs/heads/{head_ref}"
    refspec = f"{rollback_target_sha}:{ref_name}"
    push = await self._deps.runner.run(
        [
            "git",
            *git_safe_directory_config_args(worktree_path),
            "-C",
            str(worktree_path),
            "push",
            f"--force-with-lease={ref_name}:{expected_remote_head_sha}",
            repo_url,
            refspec,
        ],
        env=git_env,
    )
    if not push.ok:
        _log.warning(
            "monitor.hosted_terminal_head_remote_rollback_failed",
            workspace_id=workspace_id,
            rollback_target_sha=rollback_target_sha,
            expected_remote_head_sha=expected_remote_head_sha,
            push_returncode=push.returncode,
            push_stderr=(push.stderr or "")[:400],
        )
        return False

    fetch = await self._deps.runner.run(
        [
            "git",
            *git_safe_directory_config_args(worktree_path),
            "-C",
            str(worktree_path),
            "fetch",
            "--no-tags",
            repo_url,
            head_ref,
        ],
        env=git_env,
    )
    if not fetch.ok:
        _log.warning(
            "monitor.hosted_terminal_head_remote_rollback_verify_fetch_failed",
            workspace_id=workspace_id,
            rollback_target_sha=rollback_target_sha,
            fetch_returncode=fetch.returncode,
            fetch_stderr=(fetch.stderr or "")[:400],
        )
        return False

    rev_parse = await self._deps.runner.run(
        [
            "git",
            *git_safe_directory_config_args(worktree_path),
            "-C",
            str(worktree_path),
            "rev-parse",
            "FETCH_HEAD",
        ],
        env=git_env,
    )
    fetched_sha = str(rev_parse.stdout).strip()
    if not rev_parse.ok or fetched_sha.lower() != rollback_target_sha.lower():
        _log.warning(
            "monitor.hosted_terminal_head_remote_rollback_verify_mismatch",
            workspace_id=workspace_id,
            rollback_target_sha=rollback_target_sha,
            fetched_sha=fetched_sha or None,
            reason_code=_HOSTED_REMOTE_HEAD_ROLLBACK_FAILED_REASON,
        )
        return False

    _log.info(
        "monitor.hosted_terminal_head_remote_rollback",
        workspace_id=workspace_id,
        rollback_target_sha=rollback_target_sha,
        rolled_back_from=expected_remote_head_sha,
    )
    return True


async def _gate_hosted_terminal_head_delta(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    base_ref: str,
    terminal_head_sha: str,
    command_evidence: Sequence[str],
) -> None:
    changed_paths = await _hosted_terminal_head_delta_paths(
        self,
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        base_ref=base_ref,
        terminal_head_sha=terminal_head_sha,
    )
    if not changed_paths:
        return

    policy_message = await self._refresh_supply_chain_policy_before_push(
        workspace_id=workspace_id,
        command_evidence=command_evidence,
        changed_paths=changed_paths,
    )
    if policy_message is not None:
        raise _MonitorPolicyBlockedError(policy_message)

    try:
        violations = await _hosted_terminal_head_protected_scope_violations(
            self,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            base_ref=base_ref,
            terminal_head_sha=terminal_head_sha,
            changed_paths=changed_paths,
        )
    except ProtectedScopeDiffError as exc:
        raise _MonitorPolicyBlockedError(
            f"protected-scope policy could not verify hosted repair terminal head: {exc}",
            reason_code=_PROTECTED_SCOPE_DIFF_UNAVAILABLE_REASON,
        ) from exc
    if violations:
        raise _MonitorPolicyBlockedError(
            quality_gate_violation_message(list(violations)),
            reason_code=_PROTECTED_SCOPE_PUSH_BLOCKED_REASON,
        )


async def _hosted_terminal_head_delta_paths(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    base_ref: str,
    terminal_head_sha: str,
) -> tuple[str, ...]:
    if base_ref.lower() == terminal_head_sha.lower():
        return ()
    diff = await self._deps.runner.run(
        [
            "git",
            *git_safe_directory_config_args(worktree_path),
            "-C",
            str(worktree_path),
            "diff",
            "--name-status",
            "-z",
            f"{base_ref}..{terminal_head_sha}",
            "--",
        ],
        env=git_env_without_object_lookup_overrides(),
    )
    if not diff.ok:
        raise AgentRunError(
            agent=self._deps.adapter.name,
            result=diff,
            reason_code=_HOSTED_REMOTE_HEAD_DELTA_UNAVAILABLE_REASON,
        )
    try:
        return _changed_paths_from_name_status_z(diff.stdout)
    except ProtectedScopeDiffError as exc:
        raise AgentRunError(
            agent=self._deps.adapter.name,
            result=CommandResult(
                returncode=1,
                stdout=diff.stdout,
                stderr=(
                    f"hosted repair terminal head delta was malformed for {workspace_id}: {exc}"
                ),
            ),
            reason_code=_HOSTED_REMOTE_HEAD_DELTA_UNAVAILABLE_REASON,
        ) from exc


async def _hosted_terminal_head_protected_scope_violations(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    base_ref: str,
    terminal_head_sha: str,
    changed_paths: Sequence[str],
) -> list[QualityGateViolation]:
    async with self._deps.session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        if workspace is None:
            raise ProtectedScopeDiffError(
                f"Workspace row {workspace_id} disappeared; cannot load owned_paths "
                "for hosted terminal-head protected-scope validation."
            )
        owned_paths = list(workspace.owned_paths)
    try:
        protected_file_diffs = await protected_file_diffs_for_committed_paths(
            self._deps.runner,
            worktree_path=worktree_path,
            base_ref=base_ref,
            changed_paths=changed_paths,
            owned_paths=owned_paths,
        )
    except RuntimeError as exc:
        raise ProtectedScopeDiffError(
            "Could not read hosted terminal-head protected-scope file contents "
            f"for {terminal_head_sha[:10]}: {exc}"
        ) from exc
    return find_protected_quality_gate_changes(
        changed_paths=tuple(changed_paths),
        owned_paths=owned_paths,
        protected_file_diffs=protected_file_diffs,
        operator_granted_paths=await self._active_operator_grant_specs(workspace_id),
    )


async def _rerun_monitor_agent_pre_launch_guards(
    self: Any,
    *,
    workspace_id: str,
    source_reason_code: str = AGENT_SERVICE_UNHEALTHY,
    service_healthy: bool | None = None,
    restart_attempts: int = 0,
) -> None:
    if await self._provider_recovery_suppresses_cli(workspace_id):
        raise ProviderRecoveryRetryError()
    await _raise_if_monitor_agent_service_recovery_was_superseded(
        self,
        workspace_id=workspace_id,
        source_reason_code=source_reason_code,
        service_healthy=service_healthy,
        restart_attempts=restart_attempts,
    )
    worktree_path = self._worktrees_root / workspace_id
    if not await repair_agent_runtime_ownership(
        logger=_log,
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        reason="monitor_agent_pre_launch",
        event_name=MONITOR_AGENT_RUNTIME_OWNERSHIP_REPAIR_EVENT_NAME,
    ):
        raise _MonitorAgentRuntimeOwnershipRepairFailedError(
            "AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED"
        )
    mirror_path = mirror_path_for_worktree(worktree_path)
    if mirror_path is None:
        return
    try:
        await repair_mirror_hooks_path(mirror_path)
    except (GitOperationError, OSError) as exc:
        repair_details = mirror_hooks_repair_failure_details(
            exc,
            repair_stage="before_recovered_monitor_agent_retry",
            mirror_path=mirror_path,
        )
        _log.warning(
            "monitor.agent_service_recovery_mirror_hooks_path_repair_failed",
            workspace_id=workspace_id,
            reason_code=_MIRROR_HOOKS_PATH_POISONED_REASON,
            **repair_details,
        )
        raise _MonitorMirrorHooksPathRepairFailedError() from exc


async def _recover_monitor_agent_service_after_error(
    self: Any,
    *,
    workspace_id: str,
    compose_project: str,
    compose_file: Path,
    exc: AgentRunError,
    restart_attempts: int,
    command_evidence: list[str] | None,
) -> int | None:
    if exc.reason_code not in _AGENT_SERVICE_TIMEOUT_REASON_CODES:
        return None
    # In hosted mode (an agent runtime executor is injected) there is no
    # docker compose agent service to probe or restart — the hosted runtime
    # owns process lifecycle. Re-raising the timeout preserves the original
    # failure reason; attempting Compose restarts here would misclassify the
    # timeout as AGENT_SERVICE_UNHEALTHY and can fail/terminate monitor
    # recovery on GKE. Local Core (executor is None) keeps the restart path.
    if self._deps.adapter.is_hosted:
        _log.warning(
            "monitor.agent_service_recovery_skipped_hosted",
            workspace_id=workspace_id,
            reason_code=exc.reason_code,
            hosted=True,
        )
        return None
    if not compose_file.is_file():
        return None
    service_healthy = await probe_agent_service_health(RuntimeInspector(), compose_project)
    classification = classify_provider_failure(
        reason_code=exc.reason_code,
        stdout=exc.result.stdout,
        stderr=exc.result.stderr,
        provider=_provider_from_error(exc),
        model=_model_from_error(exc),
        service_healthy=service_healthy,
    )
    if classification is None or classification.reason_code != AGENT_SERVICE_UNHEALTHY:
        return None
    append_command_evidence(
        command_evidence,
        stdout=exc.result.stdout,
        stderr=exc.result.stderr,
    )
    return await _restart_monitor_agent_service_or_fail(
        self,
        workspace_id=workspace_id,
        compose_project=compose_project,
        compose_file=compose_file,
        exc=exc,
        service_healthy=service_healthy,
        restart_attempts=restart_attempts,
    )


async def _recover_monitor_agent_service_after_cleanup_error(
    self: Any,
    *,
    workspace_id: str,
    compose_project: str,
    compose_file: Path,
    exc: ComposeExecCleanupError,
    restart_attempts: int,
    command_evidence: list[str] | None,
    operation_start_head: str | None,
) -> int | None:
    # In hosted mode (an agent runtime executor is injected) there is no
    # docker compose agent service to probe or restart — the hosted runtime
    # owns process lifecycle. Re-raising preserves the original cleanup
    # failure reason; probing/restarting Compose here would remap a hosted
    # ComposeExecCleanupError to AGENT_SERVICE_UNHEALTHY and can fail/terminate
    # monitor recovery on GKE. Local Core (executor is None) keeps the
    # recovery path. Mirrors the hosted guard in
    # _recover_monitor_agent_service_after_error.
    if self._deps.adapter.is_hosted:
        _log.warning(
            "monitor.agent_service_recovery_skipped_hosted",
            workspace_id=workspace_id,
            reason_code=exc.reason_code,
            hosted=True,
        )
        return None
    service_healthy = await probe_agent_service_health(RuntimeInspector(), compose_project)
    if service_healthy is not False or not _cleanup_failure_indicates_agent_service_down(exc):
        return None
    cleanup_result = exc.cleanup_result
    append_command_evidence(
        command_evidence,
        stdout=cleanup_result.stdout if cleanup_result is not None else "",
        stderr=cleanup_result.stderr if cleanup_result is not None else str(exc),
    )
    await _raise_if_monitor_agent_service_recovery_was_superseded(
        self,
        workspace_id=workspace_id,
        source_reason_code=exc.reason_code,
        service_healthy=service_healthy,
        restart_attempts=restart_attempts + 1,
    )
    await _repair_monitor_git_after_recoverable_agent_cleanup_failure(
        self,
        workspace_id=workspace_id,
        operation_start_head=operation_start_head,
        command_evidence=command_evidence,
    )
    return await _restart_monitor_agent_service_or_fail(
        self,
        workspace_id=workspace_id,
        compose_project=compose_project,
        compose_file=compose_file,
        exc=exc,
        service_healthy=service_healthy,
        restart_attempts=restart_attempts,
    )


async def _repair_monitor_git_after_recoverable_agent_cleanup_failure(
    self: Any,
    *,
    workspace_id: str,
    operation_start_head: str | None,
    command_evidence: list[str] | None,
) -> None:
    worktree_path = self._worktrees_root / workspace_id
    mirror_path = mirror_path_for_worktree(worktree_path)
    if mirror_path is not None:
        try:
            await repair_mirror_hooks_path(mirror_path)
        except (GitOperationError, OSError) as exc:
            repair_details = mirror_hooks_repair_failure_details(
                exc,
                repair_stage="after_monitor_agent_cleanup_failure",
                mirror_path=mirror_path,
            )
            _log.warning(
                "monitor.agent_cleanup_mirror_hooks_path_repair_failed",
                workspace_id=workspace_id,
                reason_code=_MIRROR_HOOKS_PATH_POISONED_REASON,
                **repair_details,
            )
            raise _MonitorMirrorHooksPathRepairFailedError() from exc

    if await verify_head_object_exists(worktree_path):
        return

    recovery_head = operation_start_head or await self._open_merge_candidate_head_sha(workspace_id)
    if recovery_head is None:
        _log.warning(
            "monitor.agent_cleanup_head_object_missing",
            workspace_id=workspace_id,
            reason_code=_HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON,
        )
        raise _MonitorHeadObjectMissingError(
            _HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON,
            f"HEAD object missing for workspace {workspace_id} after agent cleanup failure",
        )
    recovered = await _recover_missing_head_object_from_filesystem(
        self,
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        operation_start_head=recovery_head,
        command_evidence=tuple(command_evidence or ()),
    )
    if recovered is None:
        _log.warning(
            "monitor.agent_cleanup_head_object_recovery_failed",
            workspace_id=workspace_id,
            recovery_head=recovery_head[:10],
            reason_code=_HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON,
        )
        raise _MonitorHeadObjectMissingError(
            _HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON,
            f"HEAD object recovery failed for workspace {workspace_id} after agent cleanup failure",
        )
    _log.info(
        "monitor.agent_cleanup_head_object_recovered",
        workspace_id=workspace_id,
        recovered_head=recovered[:10],
        reason_code=_HEAD_OBJECT_MISSING_RECOVERED_REASON,
    )


async def _restart_monitor_agent_service_or_fail(
    self: Any,
    *,
    workspace_id: str,
    compose_project: str,
    compose_file: Path,
    exc: AgentRunError | ComposeExecCleanupError,
    service_healthy: bool | None,
    restart_attempts: int,
) -> int:
    if restart_attempts >= _AGENT_SERVICE_RESTART_ATTEMPTS:
        await _terminate_monitor_for_unhealthy_agent_service(
            self,
            workspace_id=workspace_id,
            exc=exc,
            service_healthy=service_healthy,
            restart_attempts=restart_attempts,
            message="agent compose service stayed unhealthy after restart attempts",
        )
    restart_attempts += 1
    await _raise_if_monitor_agent_service_recovery_was_superseded(
        self,
        workspace_id=workspace_id,
        source_reason_code=exc.reason_code,
        service_healthy=service_healthy,
        restart_attempts=restart_attempts,
    )
    manager = ComposeManager(
        work_dir=self._work_dir,
        template_path=_monitor_agent_service_recovery_template_sentinel(self._work_dir),
    )
    compose_up_timeout_seconds = await _monitor_agent_service_restart_timeout_seconds(
        self,
        workspace_id=workspace_id,
    )
    try:
        await manager.ensure_project_up(
            project_name=compose_project,
            compose_file=compose_file,
            workspace_id=workspace_id,
            wait=True,
            compose_up_timeout_seconds=compose_up_timeout_seconds,
            force_recreate=True,
            services=(),
        )
    except ComposeOperationError as restart_exc:
        await _terminate_monitor_for_unhealthy_agent_service(
            self,
            workspace_id=workspace_id,
            exc=exc,
            service_healthy=service_healthy,
            restart_attempts=restart_attempts,
            message=f"agent compose service restart failed: {restart_exc!r}"[:2000],
        )
    _log.warning(
        "monitor.agent_service_restarted",
        workspace_id=workspace_id,
        compose_project=compose_project,
        restart_attempts=restart_attempts,
        reason_code=AGENT_SERVICE_UNHEALTHY,
    )
    await _raise_if_monitor_agent_service_recovery_was_superseded(
        self,
        workspace_id=workspace_id,
        source_reason_code=exc.reason_code,
        service_healthy=service_healthy,
        restart_attempts=restart_attempts,
    )
    return restart_attempts


async def _raise_if_monitor_agent_service_recovery_was_superseded(
    self: Any,
    *,
    workspace_id: str,
    source_reason_code: str,
    service_healthy: bool | None,
    restart_attempts: int,
) -> None:
    async with self._deps.session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        if workspace is None:
            message = "agent compose service recovery superseded: workspace disappeared"
            _log.warning(
                "monitor.agent_service_recovery_superseded",
                workspace_id=workspace_id,
                reason="workspace_missing",
            )
            raise _MonitorAgentServiceRecoverySupersededError(
                message,
                reason_code=AGENT_SERVICE_UNHEALTHY,
                details=_agent_service_recovery_source_details(
                    source_reason_code=source_reason_code,
                    service_healthy=service_healthy,
                    restart_attempts=restart_attempts,
                    superseded_reason="workspace_missing",
                ),
            )
        if workspace.status != WorkspaceStatus.monitoring_pr.value:
            message = "agent compose service recovery superseded: workspace left monitoring_pr"
            _log.warning(
                "monitor.agent_service_recovery_superseded",
                workspace_id=workspace_id,
                reason="status_changed",
                status=workspace.status,
            )
            raise _MonitorAgentServiceRecoverySupersededError(
                message,
                reason_code=AGENT_SERVICE_UNHEALTHY,
                details=_agent_service_recovery_source_details(
                    source_reason_code=source_reason_code,
                    service_healthy=service_healthy,
                    restart_attempts=restart_attempts,
                    superseded_reason="status_changed",
                ),
            )
        monitor_owner_id = getattr(self, "_monitor_owner_id", None)
        superseded_claimed_runner = (
            monitor_owner_id is not None and workspace.monitor_claimed_by != monitor_owner_id
        )
        superseded_inline_handoff = (
            monitor_owner_id is None and workspace.monitor_claimed_by is not None
        )
        if superseded_claimed_runner or superseded_inline_handoff:
            message = "agent compose service recovery superseded: monitor claim changed"
            _log.warning(
                "monitor.agent_service_recovery_superseded",
                workspace_id=workspace_id,
                reason="monitor_claim_changed",
                monitor_owner_id=monitor_owner_id,
                monitor_claimed_by=workspace.monitor_claimed_by,
            )
            raise _MonitorAgentServiceRecoverySupersededError(
                message,
                reason_code=AGENT_SERVICE_UNHEALTHY,
                details=_agent_service_recovery_source_details(
                    source_reason_code=source_reason_code,
                    service_healthy=service_healthy,
                    restart_attempts=restart_attempts,
                    superseded_reason="monitor_claim_changed",
                ),
            )


async def _monitor_agent_service_restart_timeout_seconds(
    self: Any,
    *,
    workspace_id: str,
) -> int:
    try:
        async with self._deps.session_factory() as session:
            workspace = await WorkspaceRepository(session).get(workspace_id)
            if workspace is None or not workspace.resolved_profile:
                return _MONITOR_AGENT_SERVICE_RESTART_TIMEOUT_SECONDS
            profile = WorkspaceProfile.model_validate_persisted(workspace.resolved_profile)
            task_policy = (
                workspace.task_policy if isinstance(workspace.task_policy, Mapping) else {}
            )
            return effective_compose_up_timeout_seconds(
                profile=profile,
                companions=companion_specs_from_task_policy(task_policy),
            )
    except (SQLAlchemyError, ValidationError):
        _log.exception(
            "monitor.agent_service_restart_timeout_resolution_failed",
            workspace_id=workspace_id,
        )
        return _MONITOR_AGENT_SERVICE_RESTART_TIMEOUT_SECONDS


def _agent_service_recovery_source_details(
    *,
    source_reason_code: str,
    service_healthy: bool | None,
    restart_attempts: int,
    superseded_reason: str | None = None,
) -> dict[str, object]:
    details: dict[str, object] = {
        "reason_code": AGENT_SERVICE_UNHEALTHY,
        "source_reason_code": source_reason_code,
        "service_healthy": service_healthy,
        "restart_attempts": restart_attempts,
    }
    if superseded_reason is not None:
        details["superseded_reason"] = superseded_reason
    return details


async def _terminate_monitor_for_unhealthy_agent_service(
    self: Any,
    *,
    workspace_id: str,
    exc: AgentRunError | ComposeExecCleanupError,
    service_healthy: bool | None,
    restart_attempts: int,
    message: str,
) -> NoReturn:
    exc_details = getattr(exc, "details", None)
    details = dict(exc_details) if isinstance(exc_details, dict) else {}
    details["provider_recovery"] = {
        "reason_code": AGENT_SERVICE_UNHEALTHY,
        "failure_type": "runtime_unhealthy",
        "failure_scope": "infra",
        "retryable": True,
        "failure_fingerprint": "",
        "fallback_allowed": False,
    }
    agent_service_recovery_details = _agent_service_recovery_source_details(
        source_reason_code=exc.reason_code,
        service_healthy=service_healthy,
        restart_attempts=restart_attempts,
    )
    details["agent_service_recovery"] = agent_service_recovery_details
    await self._terminate_failed(
        workspace_id,
        message=message,
        reason_code=AGENT_SERVICE_UNHEALTHY,
        details=details,
    )
    raise _MonitorAgentServiceRecoveryFailedError(
        message,
        reason_code=AGENT_SERVICE_UNHEALTHY,
        details=agent_service_recovery_details,
    )


def _monitor_agent_service_recovery_template_sentinel(work_dir: Path) -> Path:
    return work_dir / "compose" / ".monitor-agent-service-recovery-does-not-render.yml.j2"


def _cleanup_failure_indicates_agent_service_down(exc: ComposeExecCleanupError) -> bool:
    if exc.reason_code != EXEC_PROCESS_CLEANUP_FAILED:
        return False
    result = exc.cleanup_result
    if result is None:
        output = str(exc)
    else:
        output = f"{result.stdout}\n{result.stderr}"
        if not output.strip():
            output = str(exc)
    normalized = output.lower()
    return (
        'service "agent" is not running' in normalized
        or "service 'agent' is not running" in normalized
    )


def _provider_from_error(exc: AgentRunError) -> str | None:
    details = exc.details if isinstance(exc.details, dict) else {}
    provider = details.get("provider")
    if isinstance(provider, str) and provider.strip():
        return provider.strip()
    provider_recovery = details.get("provider_recovery")
    if isinstance(provider_recovery, dict):
        recovery_provider = provider_recovery.get("provider")
        if isinstance(recovery_provider, str) and recovery_provider.strip():
            return recovery_provider.strip()
    return None


def _model_from_error(exc: AgentRunError) -> str | None:
    details = exc.details if isinstance(exc.details, dict) else {}
    model = details.get("model")
    if isinstance(model, str) and model.strip():
        return model.strip()
    provider_recovery = details.get("provider_recovery")
    if isinstance(provider_recovery, dict):
        recovery_model = provider_recovery.get("model")
        if isinstance(recovery_model, str) and recovery_model.strip():
            return recovery_model.strip()
    return None
