"""Hosted execution helpers for agent adapters."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import awf.adapters.base as base_module
from awf.adapters.base_hosted_identity import (
    _hosted_identity_int,
    _hosted_identity_str,
    _hosted_identity_str_tuple,
)
from awf.adapters.failure_reasons import _failure_reason_for_result
from awf.adapters.provider_failures import classify_provider_failure
from awf.adapters.run_results import AgentRunError, AgentRunResult
from awf.adapters.runtime_executor import (
    _HOSTED_TIMEOUT_REASONS,
    _HOSTED_TIMEOUT_RETURN_CODE,
    AgentRuntimeExecRequest,
    AgentRuntimeExecResult,
    AgentRuntimeGitPreparation,
)
from awf.common.commands import CommandResult, StreamCallback
from awf.common.logging import get_logger
from awf.profiles.models import WorkspaceProfile

if TYPE_CHECKING:
    from awf.adapters.base import AgentAdapter

_log = get_logger(__name__)

_HOSTED_CANCEL_DRAIN_TIMEOUT_SECONDS = 1.0
"""Maximum time to wait for hosted executor cleanup after adapter timeout."""

_HOSTED_FILE_BACKED_ENV_ONLY_UNSUPPORTED_NAMES = frozenset(
    {
        # ADC is a filesystem path whose local Compose contract includes an auth
        # bind mount. Hosted requests currently carry env values only, so profile
        # passthrough must not re-add the name after adapters omit it.
        "GOOGLE_APPLICATION_CREDENTIALS",
    }
)


async def build_hosted_exec_request(
    adapter: AgentAdapter,
    *,
    compose_file: Path,
    compose_project: str,
    prompt_input: bytes,
    cli_args: list[str],
    selected_model: str | None,
    workspace_id: str | None,
    log_source: str,
    hosted_pr_identity: dict[str, Any] | None,
    git_preparation: AgentRuntimeGitPreparation | None,
    profile: WorkspaceProfile | None,
    worktree_path: Path | None,
    read_only: bool,
    on_stdout_cb: StreamCallback | None,
    on_stderr_cb: StreamCallback | None,
) -> AgentRuntimeExecRequest:
    """Build an AgentRuntimeExecRequest for hosted execution."""
    compose_env, postgres_passwords = await asyncio.to_thread(
        base_module.try_compose_agent_env_and_postgres_passwords,
        compose_file,
        worker_env=os.environ,
    )
    env_passthrough_names: tuple[str, ...]
    env_passthrough_aliases: tuple[tuple[str, str], ...]
    profile_env: tuple[tuple[str, str], ...]
    file_auth_mount_targets: tuple[str, ...]
    if compose_env is None:
        env_passthrough_names = ()
        env_passthrough_aliases = ()
        profile_env = ()
        file_auth_mount_targets = ()
    else:
        env_passthrough_names = await asyncio.to_thread(
            base_module.filter_hosted_env_passthrough_names,
            adapter.hosted_env_passthrough_names,
            compose_file=compose_file,
            compose_env=compose_env,
        )
        profile_env_passthrough_names = await asyncio.to_thread(
            base_module.hosted_profile_env_passthrough_names,
            compose_file,
            compose_env=compose_env,
        )
        env_passthrough_aliases = await asyncio.to_thread(
            base_module.hosted_profile_env_passthrough_aliases,
            compose_file,
            compose_env=compose_env,
        )
        if profile_env_passthrough_names:
            existing_names = set(env_passthrough_names)
            env_passthrough_names = env_passthrough_names + tuple(
                name
                for name in profile_env_passthrough_names
                if name not in existing_names
                and name not in _HOSTED_FILE_BACKED_ENV_ONLY_UNSUPPORTED_NAMES
            )
        github_token_names = await asyncio.to_thread(
            base_module.hosted_github_token_passthrough_names,
            compose_file,
            compose_env=compose_env,
        )
        if github_token_names:
            existing_names = set(env_passthrough_names)
            alias_targets = {target for target, _source in env_passthrough_aliases}
            alias_sources = {source for _target, source in env_passthrough_aliases}
            env_passthrough_names = env_passthrough_names + tuple(
                name
                for name in github_token_names
                if name not in existing_names
                and name not in alias_targets
                and name not in alias_sources
            )
        profile_env = await asyncio.to_thread(
            base_module.literal_profile_env_from_compose,
            compose_file,
            compose_env=compose_env,
            postgres_passwords=postgres_passwords,
        )
        file_auth_mount_targets = await asyncio.to_thread(
            base_module.hosted_file_auth_mount_targets,
            compose_file,
            compose_env=compose_env,
        )

    request = AgentRuntimeExecRequest(
        workspace_id=workspace_id,
        agent_runtime=adapter.name,
        cli_args=tuple(cli_args),
        prompt_stdin=prompt_input,
        log_source=log_source,
        model=selected_model,
        effort=adapter._default_effort,
        env_passthrough_names=env_passthrough_names,
        env_passthrough_aliases=env_passthrough_aliases,
        file_auth_mount_targets=file_auth_mount_targets,
        profile_env=profile_env,
        profile=profile,
        compose_project=compose_project,
        compose_file=compose_file,
        worktree_path=worktree_path,
        wall_timeout_seconds=adapter._agent_wall_timeout_seconds,
        idle_timeout_seconds=adapter._agent_idle_timeout_seconds,
        repo_url=_hosted_identity_str(hosted_pr_identity, "repo_url"),
        pr_url=_hosted_identity_str(hosted_pr_identity, "pr_url"),
        pr_number=_hosted_identity_int(hosted_pr_identity, "pr_number"),
        base_ref=_hosted_identity_str(hosted_pr_identity, "base_ref"),
        head_ref=_hosted_identity_str(hosted_pr_identity, "head_ref"),
        head_repo_url=_hosted_identity_str(hosted_pr_identity, "head_repo_url"),
        head_repo_slug=_hosted_identity_str(hosted_pr_identity, "head_repo_slug"),
        owned_paths=_hosted_identity_str_tuple(hosted_pr_identity, "owned_paths"),
        expected_head_sha=_hosted_identity_str(hosted_pr_identity, "expected_head_sha"),
        git_preparation=git_preparation,
        read_only=read_only,
        on_stdout=on_stdout_cb,
        on_stderr=on_stderr_cb,
    )
    _log.info(
        "agent.run.hosted.start",
        agent=adapter.name_str,
        workspace_id=workspace_id,
        model=selected_model,
        effort=adapter._default_effort,
        wall_timeout_seconds=adapter._agent_wall_timeout_seconds,
        idle_timeout_seconds=adapter._agent_idle_timeout_seconds,
        source=log_source,
        prompt_bytes=len(prompt_input),
        env_passthrough_names=list(env_passthrough_names),
        env_passthrough_aliases=[
            {"target": target, "source": source} for target, source in env_passthrough_aliases
        ],
        file_auth_mount_targets=list(file_auth_mount_targets),
        profile_env_keys=[key for key, _ in profile_env],
    )
    return request


def classify_hosted_result(
    adapter: AgentAdapter,
    *,
    hosted_result: AgentRuntimeExecResult,
    model: str | None,
    workspace_id: str | None,
) -> AgentRunResult:
    """Map a hosted executor result through failure classification."""
    timeout_reason = (
        hosted_result.timeout_reason
        if hosted_result.returncode == _HOSTED_TIMEOUT_RETURN_CODE
        and hosted_result.timeout_reason in _HOSTED_TIMEOUT_REASONS
        else None
    )
    command_result = CommandResult(
        returncode=hosted_result.returncode,
        stdout=hosted_result.stdout,
        stderr=hosted_result.stderr,
        reason_code=timeout_reason,
    )
    if command_result.ok:
        _log.info(
            "agent.run.hosted.ok",
            agent=adapter.name_str,
            workspace_id=workspace_id,
            stdout_bytes=len(command_result.stdout),
            stderr_bytes=len(command_result.stderr),
        )
        return AgentRunResult(
            returncode=command_result.returncode,
            stdout=command_result.stdout,
            stderr=command_result.stderr,
            terminal_head_sha=hosted_result.terminal_head_sha,
        )
    provider = adapter.get_provider(model)
    selected_model = adapter._selected_model_for_run(model=model)
    reported_model = selected_model or "unknown"
    base_reason = _failure_reason_for_result(command_result)
    provider_failure = classify_provider_failure(
        reason_code=base_reason,
        stdout=command_result.stdout,
        stderr=command_result.stderr,
        provider=provider,
        model=selected_model,
    )
    reason_code = provider_failure.reason_code if provider_failure is not None else base_reason
    log_event = (
        "agent.run.hosted.timeout"
        if reason_code in {"AGENT_TIMEOUT", "AGENT_IDLE_TIMEOUT"}
        else "agent.run.hosted.failed"
    )
    _log.warning(
        log_event,
        agent=adapter.name_str,
        workspace_id=workspace_id,
        returncode=command_result.returncode,
        reason_code=reason_code,
        stdout_bytes=len(command_result.stdout),
        stderr_bytes=len(command_result.stderr),
    )
    details: dict[str, str | bool | int | dict[str, object]] | None = None
    if provider_failure is not None:
        recovery_metadata = provider_failure.to_metadata()
        details = {
            "provider": recovery_metadata.get("provider", provider),
            "model": recovery_metadata.get("model", reported_model),
            "retryable": True,
            "recommended_action": str(recovery_metadata["recommended_action"]),
            "provider_recovery": recovery_metadata,
        }
    if hosted_result.terminal_head_sha:
        if details is None:
            details = {}
        details["terminal_head_sha"] = hosted_result.terminal_head_sha
    raise AgentRunError(
        agent=adapter.name,
        result=command_result,
        reason_code=reason_code,
        details=details,
    )
