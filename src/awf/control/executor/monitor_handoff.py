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
import tempfile as tempfile
import time as time
import traceback as traceback
from collections.abc import Callable, Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any, cast

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode, ScalarNode

from awf.adapters.base import get_adapter
from awf.common.audit import redact_audit_text
from awf.common.github_client import (
    GitHubClient,
    GitHubClientError,
    PullRequestMetadataError,
    RepoRef,
)
from awf.control.executor.constants import (
    _DEPRECATED_TASK_KIND_REASON_CODE,
    _EXECUTOR_AUDIT_ACTOR,
    _PR_ADOPTION_METADATA_MISSING_REASON_CODE,
    _PR_ADOPTION_MONITOR_UNAVAILABLE_REASON_CODE,
    _PR_ADOPTION_SKIP_AGENT_REASON_CODE,
    _PR_MONITOR_ADOPTED_EVENT,
    _PR_MONITOR_ADOPTED_REASON_CODE,
    _RELEASE_SYNC_GITHUB_ERROR_REASON_CODE,
    _RELEASE_SYNC_NO_CHANGES_EVENT,
    _RELEASE_SYNC_REPO_INVALID_REASON_CODE,
    _SUPPORTED_TASK_KINDS,
    _UNSUPPORTED_TASK_KIND_REASON_CODE,
    SETUP_DEPENDENCY_NETWORK_RETRY_EVENT_TYPE,
    SETUP_DEPENDENCY_NETWORK_RETRY_EXHAUSTED_EVENT_TYPE,
)
from awf.control.executor.helpers import (
    _agent_defaults_for_workspace,
    _call_pr_monitor_factory,
    _missing_monitor_recovery_metadata,
    _missing_sync_feature_pr_adoption_metadata,
    _profile_for_workspace,
    _provider_recovery_default_model_for_monitor_handoff,
    _redacted_exception_traceback,
    _release_sync_source_branch,
    _release_sync_target_branch,
    _required_metadata_str,
    _sync_feature_pr_adoption_metadata,
    _sync_feature_pr_missing_metadata_message,
    _with_release_sync_pr_metadata,
)
from awf.control.executor.logging_ops import (
    _setup_dependency_network_details,
    _setup_dependency_network_event_payload,
)
from awf.control.executor.metadata import _metadata_int
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
    optional_env_secret_compose_placeholder,
)
from awf.node.compose_manager import ComposeOperationError
from awf.node.stack_launcher import effective_compose_up_timeout_seconds
from awf.runtime.release_pr_sync import (
    NO_CHANGES_REASON_CODE,
    ReleasePrSyncError,
    ReleasePrSyncNoOp,
    prepare_release_pr_sync,
    release_pr_body,
    release_pr_title,
)
from awf.runtime.validation import (
    SETUP_DEPENDENCY_NETWORK_RETRY,
    SETUP_DEPENDENCY_NETWORK_RETRY_EXHAUSTED,
    ValidationResult,
)


class _ComposeInterpolationPreservingDumper(yaml.SafeDumper):
    """Safe YAML dumper that keeps Compose interpolation scalars active."""


class _ComposeStringKeySafeLoader(yaml.SafeLoader):
    """Safe YAML loader that keeps Compose scalar mapping keys as strings."""


def _construct_compose_string_key_mapping(
    loader: _ComposeStringKeySafeLoader,
    node: MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    if not isinstance(node, MappingNode):
        raise ConstructorError(
            None,
            None,
            f"expected a mapping node, but found {node.id}",
            node.start_mark,
        )
    loader.flatten_mapping(node)
    construct_object = cast(Callable[..., Any], loader.construct_object)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key: Any
        if isinstance(key_node, ScalarNode):
            key = key_node.value
        else:
            key = construct_object(key_node, deep=deep)
        try:
            hash(key)
        except TypeError as exc:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found unhashable key",
                key_node.start_mark,
            ) from exc
        mapping[key] = construct_object(
            value_node,
            deep=deep,
        )
    return mapping


_ComposeStringKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_compose_string_key_mapping,
)


def _represent_compose_interpolation_string(
    dumper: _ComposeInterpolationPreservingDumper,
    value: str,
) -> Any:
    style = '"' if "${" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_ComposeInterpolationPreservingDumper.add_representer(
    str,
    _represent_compose_interpolation_string,
)


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
    """Resume the PR monitor for a workspace already in ``monitoring_pr``."""

    ws = await self._load_workspace(workspace_id)
    if ws is None:
        _log.warning("executor.resume_skip_unknown", workspace_id=workspace_id)
        return
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
    await monitor.run(
        workspace_id=workspace_id,
        compose_project=compose_project,
        compose_file=Path(compose_file_path),
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


def _refresh_optional_companion_env_secrets_for_resume(
    *,
    workspace_id: str,
    compose_file: Path,
    companion_specs: tuple[WorkspaceCompanionSpec, ...],
    environ: Mapping[str, str],
) -> None:
    """Refresh optional companion env-secret targets before compose resume."""
    missing_targets = _missing_optional_companion_env_secret_targets(
        companion_specs=companion_specs,
        environ=environ,
    )
    present_refs = _present_optional_companion_env_secret_refs(
        companion_specs=companion_specs,
        environ=environ,
    )
    if not missing_targets and not present_refs:
        return

    try:
        payload = _safe_load_compose_payload_for_resume(compose_file.read_text(encoding="utf-8"))
    except OSError:
        _log.warning(
            "executor.resume_companion_env_secret_refresh_read_failed",
            workspace_id=workspace_id,
            compose_file=str(compose_file),
        )
        return
    except yaml.YAMLError:
        _log.warning(
            "executor.resume_companion_env_secret_refresh_parse_failed",
            workspace_id=workspace_id,
            compose_file=str(compose_file),
        )
        return

    # This best-effort resume repair uses PyYAML, which preserves Compose
    # interpolation via the custom dumper but not comments or block-scalar style.
    removed_count = _remove_compose_environment_targets(payload, missing_targets)
    restored_count = _restore_compose_environment_refs(payload, present_refs)
    if removed_count == 0 and restored_count == 0:
        return

    try:
        _atomic_write_text(
            compose_file,
            _safe_dump_compose_payload_for_resume(payload),
            encoding="utf-8",
        )
    except OSError:
        _log.warning(
            "executor.resume_companion_env_secret_refresh_write_failed",
            workspace_id=workspace_id,
            compose_file=str(compose_file),
        )
        return
    _log.warning(
        "executor.resume_companion_env_secret_refresh_reformatted",
        workspace_id=workspace_id,
        compose_file=str(compose_file),
        removed_count=removed_count,
        restored_count=restored_count,
        detail=(
            "optional companion env-secret refresh rewrote the compose file; "
            "comments, block-scalar style, and explicit null markers are not preserved"
        ),
    )
    if removed_count:
        _log.info(
            "executor.resume_companion_optional_env_secrets_omitted",
            workspace_id=workspace_id,
            compose_file=str(compose_file),
            omitted_count=removed_count,
        )
    if restored_count:
        _log.info(
            "executor.resume_companion_optional_env_secrets_restored",
            workspace_id=workspace_id,
            compose_file=str(compose_file),
            restored_count=restored_count,
        )


def _safe_dump_compose_payload_for_resume(payload: object) -> str:
    return yaml.dump(
        payload,
        Dumper=_ComposeInterpolationPreservingDumper,
        sort_keys=False,
    )


def _safe_load_compose_payload_for_resume(text: str) -> object:
    return yaml.load(text, Loader=_ComposeStringKeySafeLoader)


def _atomic_write_text(path: Path, text: str, *, encoding: str) -> None:
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding=encoding,
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp_file:
            tmp_path = Path(tmp_file.name)
            tmp_file.write(text)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        assert tmp_path is not None
        tmp_path.replace(path)
    except OSError:
        if tmp_path is not None:
            with suppress(OSError):
                tmp_path.unlink(missing_ok=True)
        raise


def _missing_optional_companion_env_secret_targets(
    *,
    companion_specs: tuple[WorkspaceCompanionSpec, ...],
    environ: Mapping[str, str],
) -> dict[str, set[str]]:
    missing_targets: dict[str, set[str]] = {}
    for spec in companion_specs:
        for secret in spec.environment_secrets:
            if secret.required or secret.provider != "env" or secret.kind != "env":
                continue
            if secret.value_from in environ:
                continue
            missing_targets.setdefault(spec.name, set()).add(secret.target)
    return missing_targets


def _present_optional_companion_env_secret_refs(
    *,
    companion_specs: tuple[WorkspaceCompanionSpec, ...],
    environ: Mapping[str, str],
) -> dict[str, dict[str, str]]:
    present_refs: dict[str, dict[str, str]] = {}
    for spec in companion_specs:
        for secret in spec.environment_secrets:
            if secret.required or secret.provider != "env" or secret.kind != "env":
                continue
            if secret.value_from not in environ:
                continue
            present_refs.setdefault(spec.name, {})[secret.target] = (
                optional_env_secret_compose_placeholder(secret.value_from)
            )
    return present_refs


def _remove_compose_environment_targets(
    payload: object,
    targets_by_service: Mapping[str, set[str]],
) -> int:
    if not isinstance(payload, dict):
        return 0
    services = payload.get("services")
    if not isinstance(services, dict):
        return 0

    removed_count = 0
    for service_name, targets in targets_by_service.items():
        service = services.get(service_name)
        if not isinstance(service, dict):
            continue
        environment = service.get("environment")
        if isinstance(environment, dict):
            for target in targets:
                if target in environment:
                    del environment[target]
                    removed_count += 1
            if not environment:
                del service["environment"]
            continue
        if isinstance(environment, list):
            retained_environment: list[object] = []
            for item in environment:
                if _compose_environment_list_item_targets(item, targets):
                    removed_count += 1
                    continue
                retained_environment.append(item)
            if len(retained_environment) != len(environment):
                if retained_environment:
                    service["environment"] = retained_environment
                else:
                    # Preserve list style so same-pass restores do not switch
                    # the section to Compose mapping form.
                    service["environment"] = []
    return removed_count


def _restore_compose_environment_refs(
    payload: object,
    refs_by_service: Mapping[str, Mapping[str, str]],
) -> int:
    if not isinstance(payload, dict):
        return 0
    services = payload.get("services")
    if not isinstance(services, dict):
        return 0

    restored_count = 0
    for service_name, refs in refs_by_service.items():
        if not refs:
            continue
        service = services.get(service_name)
        if not isinstance(service, dict):
            continue
        environment = service.get("environment")
        if isinstance(environment, dict):
            for target, ref in refs.items():
                if environment.get(target) != ref:
                    environment[target] = ref
                    restored_count += 1
            continue
        if isinstance(environment, list):
            restored_count += _restore_compose_environment_list_refs(environment, refs)
            continue
        if environment is None:
            service["environment"] = dict(refs)
            restored_count += len(refs)
    return restored_count


def _restore_compose_environment_list_refs(
    environment: list[object],
    refs: Mapping[str, str],
) -> int:
    restored_count = 0
    seen_targets: set[str] = set()
    restored_targets: set[str] = set()
    for index, item in enumerate(environment):
        if not isinstance(item, str):
            continue
        key = item.split("=", 1)[0]
        ref = refs.get(key)
        if ref is None:
            continue
        seen_targets.add(key)
        replacement = f"{key}={ref}"
        if item != replacement:
            environment[index] = replacement
            if key not in restored_targets:
                restored_targets.add(key)
                restored_count += 1

    for target, ref in refs.items():
        if target in seen_targets:
            continue
        environment.append(f"{target}={ref}")
        restored_count += 1
    return restored_count


def _compose_environment_list_item_targets(item: object, targets: set[str]) -> bool:
    if not isinstance(item, str):
        return False
    key = item.split("=", 1)[0]
    return key in targets


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
        source_head_sha=source_head_sha,
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
) -> _MonitorRunnerProto | None:
    """Build the PR monitor for a handoff, marking the workspace failed on error.

    Shared by the ``sync_feature_pr`` and ``sync_release_pr`` handoffs. Returns
    ``None`` (after transitioning to ``failed``) when no monitor can be built.
    """
    monitor: _MonitorRunnerProto | None = self._pr_monitor
    if monitor is None and self._pr_monitor_factory is None:
        await self._mark_failed(
            workspace_id=workspace_id,
            from_status=WorkspaceStatus.running,
            failure_reason=FailureReason.infrastructure_failure,
            message=f"{build_failed_message_prefix}no PR monitor configured",
            reason_code=_PR_ADOPTION_MONITOR_UNAVAILABLE_REASON_CODE,
        )
        return None

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
        await self._mark_failed(
            workspace_id=workspace_id,
            from_status=WorkspaceStatus.running,
            failure_reason=FailureReason.infrastructure_failure,
            message=f"{build_failed_message_prefix}no PR monitor configured",
            reason_code=_PR_ADOPTION_MONITOR_UNAVAILABLE_REASON_CODE,
        )
        return None
    return monitor


async def _handoff_sync_release_pr_monitor(
    self: Any,
    *,
    workspace_id: str,
    workspace: Workspace,
    compose_project: str,
    compose_file: Path,
    worktree_path: Path,
) -> None:
    """Open/reuse a source→target release PR, then monitor it (never auto-merge).

    No coding agent, no feature PR. When the source branch has no commits
    ahead of the target, the workspace completes cleanly without a PR.
    """
    if not await self._ensure_worktree_available(
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        expected=WorkspaceStatus.running,
        action="sync_release_pr_handoff",
    ):
        return

    source_branch = _release_sync_source_branch(workspace)
    target_branch = _release_sync_target_branch(workspace)
    try:
        repo = RepoRef.from_url(workspace.repo_url)
    except ValueError as exc:
        await self._mark_failed(
            workspace_id=workspace_id,
            from_status=WorkspaceStatus.running,
            failure_reason=FailureReason.infrastructure_failure,
            message=f"sync_release_pr cannot parse repo URL: {redact_audit_text(str(exc))}",
            reason_code=_RELEASE_SYNC_REPO_INVALID_REASON_CODE,
        )
        return

    try:
        outcome = await prepare_release_pr_sync(
            runner=self._runner,
            gh=GitHubClient(self._runner),
            repo=repo,
            cwd=str(worktree_path),
            source_branch=source_branch,
            target_branch=target_branch,
            title=release_pr_title(source_branch=source_branch, target_branch=target_branch),
            body=release_pr_body(source_branch=source_branch, target_branch=target_branch),
        )
    except (ReleasePrSyncError, PullRequestMetadataError) as exc:
        await self._mark_failed(
            workspace_id=workspace_id,
            from_status=WorkspaceStatus.running,
            failure_reason=FailureReason.infrastructure_failure,
            message=f"sync_release_pr failed: {exc.message}",
            reason_code=exc.reason_code,
            details=exc.detail,
        )
        return
    except GitHubClientError as exc:
        await self._mark_failed(
            workspace_id=workspace_id,
            from_status=WorkspaceStatus.running,
            failure_reason=FailureReason.infrastructure_failure,
            message=f"sync_release_pr GitHub error ({exc.operation}): {exc.stderr or str(exc)}",
            reason_code=_RELEASE_SYNC_GITHUB_ERROR_REASON_CODE,
        )
        return

    if isinstance(outcome, ReleasePrSyncNoOp):
        await self._complete_release_pr_sync_no_op(
            workspace_id=workspace_id,
            source_branch=source_branch,
            target_branch=target_branch,
        )
        return

    monitor = await self._build_handoff_pr_monitor(
        workspace_id=workspace_id,
        workspace=workspace,
        worktree_path=worktree_path,
        compose_project=compose_project,
        compose_file=compose_file,
        build_failed_log_event="executor.sync_release_pr_monitor_build_failed",
        build_failed_message_prefix="release PR monitor handoff failed: ",
    )
    if monitor is None:
        return

    metadata = outcome.metadata
    async with self._session_factory() as session:
        repo_db = WorkspaceRepository(session)
        persisted = await repo_db.get(workspace_id)
        if persisted is None:  # pragma: no cover - destroyed mid-flight
            return
        if persisted.status != WorkspaceStatus.running.value:
            await self._record_stale_action_skip(
                repo_db,
                persisted,
                action="sync_release_pr_handoff",
                expected=WorkspaceStatus.running,
                reason_code="EXECUTOR_STALE_STATUS",
            )
            await session.commit()
            return

        persisted.pr_url = metadata.url
        persisted.pr_number = metadata.number
        persisted.remote_push_branch = source_branch
        persisted.monitor_last_commit_sha = metadata.head_sha
        persisted.base_commit = metadata.base_sha
        persisted.task_policy = _with_release_sync_pr_metadata(
            persisted.task_policy,
            metadata=metadata,
            created=outcome.created,
        )
        await repo_db.add_event(
            persisted,
            event_type=_PR_MONITOR_ADOPTED_EVENT,
            reason_code=_PR_MONITOR_ADOPTED_REASON_CODE,
            payload={
                "pr_number": metadata.number,
                "pr_url": metadata.url,
                "head_ref": metadata.head_ref,
                "base_ref": metadata.base_ref,
                "head_sha": metadata.head_sha,
                "base_sha": metadata.base_sha,
                "source_branch": source_branch,
                "target_branch": target_branch,
                "created": outcome.created,
                "source": "release_pr_sync",
            },
        )
        await repo_db.transition(
            persisted,
            to=WorkspaceStatus.validating,
            reason_code=_PR_ADOPTION_SKIP_AGENT_REASON_CODE,
            payload={"source": "release_pr_sync"},
        )
        await repo_db.transition(
            persisted,
            to=WorkspaceStatus.monitoring_pr,
            reason_code=_PR_MONITOR_ADOPTED_REASON_CODE,
            payload={
                "pr_number": metadata.number,
                "pr_url": metadata.url,
                "head_sha": metadata.head_sha,
                "base_sha": metadata.base_sha,
                "source": "release_pr_sync",
            },
        )
        await session.commit()

    _log.info(
        "executor.sync_release_pr_handoff_to_monitor",
        workspace_id=workspace_id,
        pr_url=metadata.url,
        created=outcome.created,
    )
    if not await self._recheck_status(
        workspace_id,
        expected=WorkspaceStatus.monitoring_pr,
        action="run_pr_monitor",
    ):
        return
    await monitor.run(
        workspace_id=workspace_id,
        compose_project=compose_project,
        compose_file=compose_file,
    )


async def _complete_release_pr_sync_no_op(
    self: Any,
    *,
    workspace_id: str,
    source_branch: str,
    target_branch: str,
) -> None:
    """Complete a ``sync_release_pr`` workspace that has nothing to sync."""
    async with self._session_factory() as session:
        repo_db = WorkspaceRepository(session)
        persisted = await repo_db.get(workspace_id)
        if persisted is None:  # pragma: no cover - destroyed mid-flight
            return
        if persisted.status != WorkspaceStatus.running.value:
            await self._record_stale_action_skip(
                repo_db,
                persisted,
                action="sync_release_pr_handoff",
                expected=WorkspaceStatus.running,
                reason_code="EXECUTOR_STALE_STATUS",
            )
            await session.commit()
            return
        await repo_db.add_event(
            persisted,
            event_type=_RELEASE_SYNC_NO_CHANGES_EVENT,
            reason_code=NO_CHANGES_REASON_CODE,
            payload={"source_branch": source_branch, "target_branch": target_branch},
        )
        await repo_db.transition(
            persisted,
            to=WorkspaceStatus.validating,
            reason_code=NO_CHANGES_REASON_CODE,
            payload={"source": "release_pr_sync"},
        )
        await repo_db.transition(
            persisted,
            to=WorkspaceStatus.completed,
            reason_code=NO_CHANGES_REASON_CODE,
            payload={
                "source_branch": source_branch,
                "target_branch": target_branch,
                "source": "release_pr_sync",
            },
        )
        await session.commit()
    _log.info(
        "executor.sync_release_pr_no_changes",
        workspace_id=workspace_id,
        source_branch=source_branch,
        target_branch=target_branch,
    )


async def _handoff_sync_feature_pr_monitor(
    self: Any,
    *,
    workspace_id: str,
    workspace: Workspace,
    compose_project: str,
    compose_file: Path,
    worktree_path: Path,
) -> None:
    metadata = _sync_feature_pr_adoption_metadata(workspace)
    missing = _missing_sync_feature_pr_adoption_metadata(workspace, metadata)
    if missing:
        await self._mark_failed(
            workspace_id=workspace_id,
            from_status=WorkspaceStatus.running,
            failure_reason=FailureReason.infrastructure_failure,
            message=_sync_feature_pr_missing_metadata_message(missing),
            reason_code=_PR_ADOPTION_METADATA_MISSING_REASON_CODE,
            details={"missing": missing},
        )
        return

    if not await self._ensure_worktree_available(
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        expected=WorkspaceStatus.running,
        action="sync_feature_pr_handoff",
    ):
        return

    monitor = await self._build_handoff_pr_monitor(
        workspace_id=workspace_id,
        workspace=workspace,
        worktree_path=worktree_path,
        compose_project=compose_project,
        compose_file=compose_file,
        build_failed_log_event="executor.sync_feature_pr_monitor_build_failed",
        build_failed_message_prefix="adopted PR monitor handoff failed: ",
    )
    if monitor is None:
        return

    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        persisted = await repo.get(workspace_id)
        if persisted is None:  # pragma: no cover - destroyed mid-flight
            return
        if persisted.status != WorkspaceStatus.running.value:
            await self._record_stale_action_skip(
                repo,
                persisted,
                action="sync_feature_pr_handoff",
                expected=WorkspaceStatus.running,
                reason_code="EXECUTOR_STALE_STATUS",
            )
            await session.commit()
            return

        persisted_metadata = _sync_feature_pr_adoption_metadata(persisted)
        missing = _missing_sync_feature_pr_adoption_metadata(
            persisted,
            persisted_metadata,
        )
        if missing:
            safe_message = redact_audit_text(
                _sync_feature_pr_missing_metadata_message(missing),
                limit=2000,
            )
            persisted.failure_reason = FailureReason.infrastructure_failure.value
            persisted.failure_message = safe_message
            await repo.transition(
                persisted,
                to=WorkspaceStatus.failed,
                reason_code=_PR_ADOPTION_METADATA_MISSING_REASON_CODE,
                payload={
                    "failure_reason": FailureReason.infrastructure_failure.value,
                    "reason_code": _PR_ADOPTION_METADATA_MISSING_REASON_CODE,
                    "message": safe_message,
                    "details": {"missing": missing},
                },
            )
            await session.commit()
            return

        head_sha = _required_metadata_str(persisted_metadata, "head_sha")
        base_sha = _required_metadata_str(persisted_metadata, "base_sha")
        head_ref = _required_metadata_str(persisted_metadata, "head_ref")
        base_ref = _required_metadata_str(persisted_metadata, "base_ref")
        pr_url = persisted.pr_url or _required_metadata_str(
            persisted_metadata,
            "pr_url",
        )
        pr_number = persisted.pr_number or _metadata_int(
            persisted_metadata,
            "pr_number",
        )
        remote_branch = persisted.remote_push_branch or head_ref

        persisted.pr_url = pr_url
        persisted.pr_number = pr_number
        persisted.remote_push_branch = remote_branch
        persisted.monitor_last_commit_sha = head_sha
        persisted.base_commit = base_sha
        await repo.add_event(
            persisted,
            event_type=_PR_MONITOR_ADOPTED_EVENT,
            reason_code=_PR_MONITOR_ADOPTED_REASON_CODE,
            payload={
                "pr_number": pr_number,
                "pr_url": pr_url,
                "head_ref": head_ref,
                "base_ref": base_ref,
                "head_sha": head_sha,
                "base_sha": base_sha,
                "remote_branch": remote_branch,
                "source": "existing_github_pr",
            },
        )
        await repo.transition(
            persisted,
            to=WorkspaceStatus.validating,
            reason_code=_PR_ADOPTION_SKIP_AGENT_REASON_CODE,
            payload={"source": "existing_github_pr"},
        )
        await repo.transition(
            persisted,
            to=WorkspaceStatus.monitoring_pr,
            reason_code=_PR_MONITOR_ADOPTED_REASON_CODE,
            payload={
                "pr_number": pr_number,
                "pr_url": pr_url,
                "head_sha": head_sha,
                "base_sha": base_sha,
                "source": "existing_github_pr",
            },
        )
        await session.commit()

    _log.info(
        "executor.sync_feature_pr_handoff_to_monitor",
        workspace_id=workspace_id,
        pr_url=workspace.pr_url,
    )
    if not await self._recheck_status(
        workspace_id,
        expected=WorkspaceStatus.monitoring_pr,
        action="run_pr_monitor",
    ):
        return
    await monitor.run(
        workspace_id=workspace_id,
        compose_project=compose_project,
        compose_file=compose_file,
    )


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
