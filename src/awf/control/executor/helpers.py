"""WorkspaceExecutor helper functions.

Mechanically extracted from the original orchestrator; behavior is unchanged.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import traceback
from collections.abc import (
    Callable,
    Mapping,
    Sequence,
)
from dataclasses import (
    replace,
)
from pathlib import Path
from typing import Any, cast

from awf.adapters.base import (
    AgentAdapter,
    AgentDefaults,
)
from awf.adapters.model_selection import selected_runtime_model_for_defaults
from awf.common.audit import (
    redact_audit_text,
    redact_audit_value,
)
from awf.common.github_client import (
    PullRequestAdoptionMetadata,
    RepoRef,
)
from awf.common.workspace_policy import release_sync_source_branch
from awf.control.executor.constants import (
    _DEFAULT_RELEASE_SYNC_TARGET_BRANCH,
    _EXCEPTION_TRACEBACK_LIMIT,
    _FILE_DIGEST_CHUNK_SIZE,
    _PR_NUMBER_RE,
    _RECOVERY_ACTIVE_OPERATION_STATUSES,
    _VALIDATION_EVIDENCE_CORE_KEYS,
    _VALIDATION_EVIDENCE_COVERAGE_PRIORITY_KEYS,
    _VALIDATION_EVIDENCE_JSON_LIMIT,
    PLAN_CONFORMANCE_UNSATISFIED,
    WORKTREE_MISSING_REASON_CODE,
)
from awf.control.executor.logging_ops import (
    _format_duration_for_message,
    _validation_evidence_size_summary,
)
from awf.control.executor.metadata import (
    _metadata_int,
    _metadata_str,
)
from awf.control.executor.protocols import _MonitorRunnerProto
from awf.control.executor.quality_gates import (
    _post_validation_conformance_failure_text,
)
from awf.control.executor.types import _PlanningRunFailure
from awf.db.enums import (
    AgentRuntime,
    FailureReason,
    OperationStatus,
    OperationType,
    TaskClass,
)
from awf.db.models import Workspace
from awf.profiles.models import WorkspaceProfile
from awf.profiles.resolver import resolve_workspace_profile
from awf.runtime.alembic_validation import (
    ALEMBIC_MIGRATION_POLICY_COMMAND,
    ALEMBIC_MIGRATION_POLICY_PHASE,
)
from awf.runtime.pr_push_remote import remote_push_url_for_workspace
from awf.runtime.validation import (
    DATABASE_GENERATED_SETUP_TIMEOUT,
    DATABASE_REFRESH_TIMEOUT,
    DB_GENERATED_SETUP_PHASE,
    PROFILE_VALIDATION_TOOL_UNAVAILABLE,
    PYTEST_TEST_FAILURE,
    ValidationCommandResult,
    ValidationCoverageResult,
    ValidationResult,
    profile_phase_command_plan,
)


def _validation_evidence_json(payload: dict[str, Any]) -> str:
    safe_payload = cast(dict[str, Any], redact_audit_value(payload))
    serialized = _serialize_validation_evidence_payload(safe_payload)
    if len(serialized) <= _VALIDATION_EVIDENCE_JSON_LIMIT:
        return serialized

    compact_payload = dict(safe_payload)
    compact_payload["evidence_truncated"] = True
    compact_payload["commands"] = _validation_evidence_size_summary(safe_payload.get("commands"))
    compact_payload["log_stream_refs"] = _validation_evidence_size_summary(
        safe_payload.get("log_stream_refs")
    )
    serialized = _serialize_validation_evidence_payload(compact_payload)
    if len(serialized) <= _VALIDATION_EVIDENCE_JSON_LIMIT:
        return serialized

    compact_payload["coverage"] = _validation_evidence_coverage_summary(
        safe_payload.get("coverage")
    )
    serialized = _serialize_validation_evidence_payload(compact_payload)
    if len(serialized) <= _VALIDATION_EVIDENCE_JSON_LIMIT:
        return serialized

    minimal_payload = {
        key: compact_payload.get(key)
        for key in _VALIDATION_EVIDENCE_CORE_KEYS
        if key in compact_payload
    }
    minimal_payload["evidence_truncated"] = True
    minimal_payload["commands"] = _validation_evidence_size_summary(safe_payload.get("commands"))
    minimal_payload["log_stream_refs"] = _validation_evidence_size_summary(
        safe_payload.get("log_stream_refs")
    )
    serialized = _serialize_validation_evidence_payload(minimal_payload)
    if len(serialized) <= _VALIDATION_EVIDENCE_JSON_LIMIT:
        return serialized

    oversized_serialized_length = len(json.dumps(safe_payload, default=str))
    floor_payload = _validation_evidence_floor_payload(
        safe_payload,
        oversized_serialized_length=oversized_serialized_length,
    )
    serialized = _serialize_validation_evidence_payload(floor_payload)
    if len(serialized) <= _VALIDATION_EVIDENCE_JSON_LIMIT:
        return serialized

    return _serialize_validation_evidence_payload(
        {
            "evidence_truncated": True,
            "truncation_reason": "validation_evidence_json_limit",
            "oversized_serialized_length": oversized_serialized_length,
        }
    )


def _serialize_validation_evidence_payload(payload: Mapping[str, Any]) -> str:
    serialized = json.dumps(payload, default=str)
    return redact_audit_text(serialized, limit=_VALIDATION_EVIDENCE_JSON_LIMIT)


def _validation_evidence_coverage_summary(value: object) -> object:
    if not isinstance(value, Mapping):
        return _validation_evidence_size_summary(value)
    summary = _validation_evidence_size_summary(value)
    for key in _VALIDATION_EVIDENCE_COVERAGE_PRIORITY_KEYS:
        if key in value:
            summary[key] = value[key]
    return summary


def _validation_evidence_floor_payload(
    payload: Mapping[str, Any],
    *,
    oversized_serialized_length: int,
) -> dict[str, Any]:
    floor_payload = {
        key: _validation_evidence_floor_value(payload[key])
        for key in _VALIDATION_EVIDENCE_CORE_KEYS
        if key in payload and key != "coverage"
    }
    if "coverage" in payload:
        floor_payload["coverage"] = _validation_evidence_size_summary(payload["coverage"])
    floor_payload["evidence_truncated"] = True
    floor_payload["truncation_reason"] = "validation_evidence_json_limit"
    floor_payload["oversized_serialized_length"] = oversized_serialized_length
    floor_payload["commands"] = _validation_evidence_size_summary(payload.get("commands"))
    floor_payload["log_stream_refs"] = _validation_evidence_size_summary(
        payload.get("log_stream_refs")
    )
    return floor_payload


def _validation_evidence_floor_value(value: object) -> object:
    if isinstance(value, str):
        if len(value) <= 512:
            return value
        return _validation_evidence_size_summary(value)
    if isinstance(value, int | float | bool) or value is None:
        return value
    return _validation_evidence_size_summary(value)


def _extract_pr_number(pr_url: str) -> int | None:
    """Parse the PR number from a GitHub PR URL.

    Matches both the canonical ``https://github.com/<owner>/<repo>/pull/123``
    and the trailing-slash variant. Returns ``None`` if the URL doesn't look
    like a PR URL — the monitor then simply won't run (executor logs a
    warning via the transition to ``monitoring_pr`` still succeeding; the
    monitor itself asserts on pr_number and terminates with a clear failure).
    """
    match = _PR_NUMBER_RE.search(pr_url)
    return int(match.group(1)) if match else None


def _worktree_missing_message(worktree_path: Path, action: str) -> str:
    return (
        f"{WORKTREE_MISSING_REASON_CODE}: managed worktree path is missing or not a "
        f"directory while preparing `{action}`: {worktree_path}"
    )


def _missing_monitor_recovery_metadata(ws: Workspace) -> list[str]:
    missing: list[str] = []
    if ws.pr_number is None:
        missing.append("pr_number")
    if not ws.pr_url:
        missing.append("pr_url")
    if not ws.remote_push_branch:
        missing.append(
            f"remote_push_branch (task_kind={ws.task_kind}, branch_name={ws.branch_name!r})"
        )
    if not ws.compose_project_name:
        missing.append("compose_project_name")
    if not ws.compose_file_path:
        missing.append("compose_file_path")
    return missing


def _sync_feature_pr_adoption_metadata(ws: Workspace) -> Mapping[str, object]:
    policy = ws.task_policy if isinstance(ws.task_policy, Mapping) else {}
    adoption = policy.get("pr_adoption")
    return adoption if isinstance(adoption, Mapping) else {}


def _release_sync_policy(ws: Workspace) -> Mapping[str, object]:
    policy = ws.task_policy if isinstance(ws.task_policy, Mapping) else {}
    block = policy.get("release_sync")
    return block if isinstance(block, Mapping) else {}


def _release_sync_source_branch(ws: Workspace) -> str:
    return release_sync_source_branch(ws.task_policy)


def _release_sync_target_branch(ws: Workspace) -> str:
    target = _nonblank_metadata_str(_release_sync_policy(ws), "target_branch")
    if target is not None:
        return target
    base = ws.branch_base
    if isinstance(base, str) and base.strip():
        return base.strip()
    return _DEFAULT_RELEASE_SYNC_TARGET_BRANCH


def _with_release_sync_pr_metadata(
    task_policy: object,
    *,
    metadata: PullRequestAdoptionMetadata,
    created: bool,
) -> dict[str, Any]:
    """Return a copy of ``task_policy`` recording the resolved release PR."""
    policy: dict[str, Any] = dict(task_policy) if isinstance(task_policy, Mapping) else {}
    block_source = policy.get("release_sync")
    block: dict[str, Any] = dict(block_source) if isinstance(block_source, Mapping) else {}
    block["pr"] = {
        "number": metadata.number,
        "url": metadata.url,
        "head_ref": metadata.head_ref,
        "base_ref": metadata.base_ref,
        "head_sha": metadata.head_sha,
        "base_sha": metadata.base_sha,
        "created": created,
    }
    policy["release_sync"] = block
    return policy


def _existing_pr_remote_push_url(ws: Workspace) -> str | None:
    if ws.task_kind != "sync_feature_pr":
        return None
    try:
        base_repo = RepoRef.from_url(ws.repo_url)
    except ValueError:
        return None
    return remote_push_url_for_workspace(ws, base_repo=base_repo)


def _missing_sync_feature_pr_adoption_metadata(
    ws: Workspace,
    metadata: Mapping[str, object],
) -> list[str]:
    missing: list[str] = []
    if ws.pr_number is None and _metadata_int(metadata, "pr_number") is None:
        missing.append("pr_number")
    if not ws.pr_url and _nonblank_metadata_str(metadata, "pr_url") is None:
        missing.append("pr_url")
    if not ws.remote_push_branch and _nonblank_metadata_str(metadata, "head_ref") is None:
        missing.append("remote_push_branch")
    for key in ("head_ref", "base_ref", "head_sha", "base_sha"):
        if _nonblank_metadata_str(metadata, key) is None:
            missing.append(f"task_policy.pr_adoption.{key}")
    return missing


def _sync_feature_pr_missing_metadata_message(missing: Sequence[str]) -> str:
    return "adopted PR workspace is missing required monitor handoff metadata: " + ", ".join(
        missing
    )


def _redacted_exception_traceback(exc: BaseException) -> str:
    formatted = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return redact_audit_text(formatted, limit=_EXCEPTION_TRACEBACK_LIMIT)


def _required_metadata_str(metadata: Mapping[str, object], key: str) -> str:
    value = _nonblank_metadata_str(metadata, key)
    if value is None:
        raise ValueError(f"missing adoption metadata key: {key}")
    return value


def _nonblank_metadata_str(metadata: Mapping[str, object], key: str) -> str | None:
    value = _metadata_str(metadata, key)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _call_pr_monitor_factory(
    factory: Callable[..., _MonitorRunnerProto],
    *,
    adapter: AgentAdapter,
    profile: WorkspaceProfile,
    workspace: Workspace,
    provider_recovery_default_model: str | None = None,
) -> _MonitorRunnerProto:
    """Call a monitor factory with the richest supported context.

    Production factories may need the persisted workspace row for monitor
    policy. Older tests and scripts predate profile/workspace-aware execution
    and expose one- or two-argument factories. We inspect arity before the call
    so a ``TypeError`` raised inside the factory body is never mistaken for an
    argument-count mismatch.
    """
    try:
        signature = inspect.signature(factory)
    except (TypeError, ValueError):
        # Without a signature, preserve the historical two-argument fallback;
        # probing by calling would risk masking TypeErrors raised inside the
        # factory body.
        return factory(adapter, profile)

    bind_errors: list[TypeError] = []
    provider_recovery_kwargs = {"provider_recovery_default_model": provider_recovery_default_model}
    for args, kwargs in (
        ((adapter, profile, workspace), provider_recovery_kwargs),
        ((adapter, profile, workspace), {}),
        ((adapter, profile), {}),
        ((adapter,), {}),
    ):
        try:
            signature.bind(*args, **kwargs)
        except TypeError as exc:
            bind_errors.append(exc)
            continue
        return factory(*args, **kwargs)

    raise bind_errors[0]


def _build_pr_body(ws: Workspace, *, defaults: AgentDefaults | None = None) -> str:
    """Standard PR description generated from the workspace's task metadata."""
    external_id = f"\n**External task ID**: {ws.task_external_id}" if ws.task_external_id else ""
    return (
        f"Automatically opened by AWF workspace `{ws.id}` "
        f"({_agent_pr_identity(ws, defaults=defaults)}).\n"
        f"{external_id}\n\n"
        f"### Task\n{ws.task_prompt}\n\n"
        f"---\nValidation: "
        f"{_validation_command_count(ws)} profile command(s) passed inside the workspace container.\n"
    )


def _agent_pr_identity(ws: Workspace, *, defaults: AgentDefaults | None = None) -> str:
    model, effort = _agent_identity_model_and_effort(ws, defaults)

    parts = [f"agent: `{ws.agent}`"]
    if model is not None:
        parts.append(f"model: `{model}`")
    if effort is not None:
        parts.append(f"effort: `{effort}`")
    return ", ".join(parts)


def _nonblank_policy_string(policy: Mapping[str, Any], key: str) -> str | None:
    value = policy.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _profile_for_workspace(
    ws: Workspace,
    *,
    worktree_path: Path,
    planning_max_iterations_default: int = 3,
) -> WorkspaceProfile:
    """Resolve the profile without stamping a runtime snapshot on the workspace.

    Executor paths that execute against a runtime-resolved profile must call the
    async resolved-profile sync before planning commands so the DB snapshot keeps
    first-write-wins semantics.
    """
    if ws.resolved_profile:
        profile = WorkspaceProfile.model_validate(ws.resolved_profile)
        return _profile_with_planning_iteration_default(
            profile,
            planning_max_iterations_default,
            raw_profile=ws.resolved_profile,
        )
    return _profile_with_planning_iteration_default(
        resolve_workspace_profile(
            worktree_path=worktree_path,
            inline_profile=ws.requested_profile,
            profile_ref=ws.profile_ref or ws.env_profile or "auto",
            validation_commands=list(ws.test_commands),
        ).profile,
        planning_max_iterations_default,
        raw_profile=ws.requested_profile,
    )


def _realign_profile_from_resolved_profile_snapshot(
    ws: Workspace,
    snapshot: dict[str, Any] | None,
    *,
    planning_max_iterations_default: int = 3,
) -> WorkspaceProfile | None:
    """Realign executor profile state from the persisted snapshot winner.

    This mutates the executor's detached ``Workspace`` object only for
    in-memory reads in the current flow. Do not pass the mutated object to
    ``session.add()`` or ``session.merge()`` afterward; SQLAlchemy would treat
    ``resolved_profile`` as dirty and could overwrite a newer database value.
    """
    if snapshot is None:
        return None
    ws.resolved_profile = snapshot
    profile = WorkspaceProfile.model_validate(snapshot)
    return _profile_with_planning_iteration_default(
        profile,
        planning_max_iterations_default,
        raw_profile=snapshot,
    )


def _profile_with_planning_iteration_default(
    profile: WorkspaceProfile,
    planning_max_iterations_default: int,
    *,
    raw_profile: Mapping[str, Any] | None = None,
) -> WorkspaceProfile:
    """Apply the settings default only when the profile omitted max_iterations."""

    explicit = (
        _raw_profile_has_explicit_planning_max_iterations(raw_profile)
        if raw_profile is not None
        else "max_iterations" in profile.planning.model_fields_set
    )
    if explicit or profile.planning.max_iterations == planning_max_iterations_default:
        return profile
    return profile.model_copy(
        deep=True,
        update={
            "planning": profile.planning.model_copy(
                update={"max_iterations": planning_max_iterations_default}
            )
        },
    )


def _raw_profile_has_explicit_planning_max_iterations(
    raw_profile: Mapping[str, Any] | None,
) -> bool:
    if raw_profile is None:
        return False
    planning = raw_profile.get("planning")
    return isinstance(planning, Mapping) and "max_iterations" in planning


def _failure_salvage_payload(
    workspace: Workspace,
    *,
    worktree_path: Path,
) -> dict[str, str]:
    branch_name = workspace.branch_name
    remote_push_branch = workspace.remote_push_branch or branch_name
    payload = {
        "hint": "Workspace worktree and branch were preserved for salvage.",
        "worktree_path": str(worktree_path),
    }
    if branch_name:
        payload["branch_name"] = branch_name
    if remote_push_branch:
        payload["remote_push_branch"] = remote_push_branch
    return payload


def _agent_identity_model_and_effort(
    ws: Workspace,
    defaults: AgentDefaults | None,
) -> tuple[str | None, str | None]:
    policy = ws.task_policy if isinstance(ws.task_policy, dict) else {}
    explicit_model = _nonblank_policy_string(policy, "agent_model")
    effort = _nonblank_policy_string(policy, "agent_effort") or (
        defaults.effort if defaults else None
    )
    model = selected_runtime_model_for_defaults(
        agent=_agent_runtime_or_none(getattr(ws, "agent", None)),
        explicit_model=explicit_model,
        default_model=defaults.model if defaults else None,
        effort=effort,
    )
    return model, effort


def _agent_runtime_or_none(agent: object) -> AgentRuntime | None:
    if isinstance(agent, AgentRuntime):
        return agent
    if isinstance(agent, str):
        try:
            return AgentRuntime(agent)
        except ValueError:
            return None
    return None


def _agent_run_model_for_workspace(ws: Workspace) -> str | None:
    """Return only the workspace's explicit model override for adapter.run()."""
    policy = ws.task_policy if isinstance(ws.task_policy, dict) else {}
    return _nonblank_policy_string(policy, "agent_model")


def _agent_defaults_for_workspace(
    ws: Workspace,
    defaults: AgentDefaults | None,
) -> AgentDefaults | None:
    """Return adapter defaults after applying workspace-persisted policy.

    Agent adapters are handed to the PR monitor, which invokes recovery
    prompts without passing an explicit ``model`` each time. Binding the
    workspace's effective model into the adapter is therefore important:
    an opencode workspace launched with ``ollama/glm-5.1:cloud`` must not
    drift back to AWF's opencode default (currently Kimi) while resolving
    PR comments.
    """
    policy = ws.task_policy if isinstance(ws.task_policy, dict) else {}
    model = _nonblank_policy_string(policy, "agent_model")
    effort = _nonblank_policy_string(policy, "agent_effort")
    if model is None and effort is None:
        return defaults
    if defaults is not None:
        return replace(
            defaults,
            model=model or defaults.model,
            effort=effort or defaults.effort,
        )
    if model is None:
        return None
    return AgentDefaults(model=model, effort=effort)


def _provider_recovery_default_model_for_monitor_handoff(
    *,
    adapter: AgentAdapter,
    defaults: AgentDefaults | None,
) -> str | None:
    """Return the default model metadata to hand to a PR monitor factory.

    Non-Cursor runtimes keep the configured runtime default so explicit task
    model failures can still fall back to that default. Cursor's portable
    effort policy can intentionally select no model for lower effort, so its
    provider recovery metadata must follow the adapter-selected implicit model.
    """
    if adapter.name is AgentRuntime.cursor:
        return adapter.provider_recovery_default_model
    return defaults.model if defaults is not None else None


def _failure_reason_for_phase(first_fail: object | None) -> FailureReason:
    phase = getattr(first_fail, "phase", None)
    reason_code = getattr(first_fail, "reason_code", None)
    if phase == "healthcheck":
        return FailureReason.health_check_failure
    if reason_code in {
        "PHASE_TIMEOUT",
        DATABASE_GENERATED_SETUP_TIMEOUT,
        DATABASE_REFRESH_TIMEOUT,
    }:
        return FailureReason.phase_timeout
    if reason_code == PROFILE_VALIDATION_TOOL_UNAVAILABLE:
        return FailureReason.profile_resolution_failure
    if phase in {"setup", "pre_agent", DB_GENERATED_SETUP_PHASE}:
        return FailureReason.service_startup_failure
    return FailureReason.validation_failure


def _validation_command_count(ws: Workspace) -> int:
    if ws.resolved_profile:
        profile = WorkspaceProfile.model_validate(ws.resolved_profile)
        coverage_count = 1 if _should_run_local_coverage(profile) else 0
        return (
            len(profile.phases.post_agent)
            + len(profile.database.pre_validation_refresh)
            + len(profile.phases.validate_commands)
            + coverage_count
        )
    return len(ws.test_commands)


def _read_text_if_present(path: Path) -> str | None:
    try:
        if path.is_file():
            text = path.read_text(encoding="utf-8").strip()
            return text or None
    except OSError:
        return None
    return None


def _digest_file_if_present(path: Path) -> str | None:
    try:
        if path.is_file():
            hasher = hashlib.sha256()
            with path.open("rb") as fh:
                while chunk := fh.read(_FILE_DIGEST_CHUNK_SIZE):
                    hasher.update(chunk)
            return hasher.hexdigest()
    except OSError:
        return None
    return None


def _digest_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _validation_run_command_records(
    *,
    profile: WorkspaceProfile,
    phase_names: tuple[str, ...],
    run_healthchecks: bool,
    coverage_evidence_status: str | None = None,
    coverage_evidence_reason_code: str | None = None,
) -> list[dict[str, Any]]:
    ordered: list[dict[str, Any]] = []
    if (
        "validate" in phase_names
        and profile.validation.alembic
        and profile.validation.alembic.enabled
    ):
        ordered.append(
            {
                "phase": ALEMBIC_MIGRATION_POLICY_PHASE,
                "command": ALEMBIC_MIGRATION_POLICY_COMMAND,
            }
        )
    command_plan = profile_phase_command_plan(profile, phase_names)
    healthcheck_before_phase = (
        "validate"
        if profile.database.pre_validation_refresh and "validate" in set(phase_names)
        else None
    )
    pending_healthchecks = list(profile.validation.healthchecks) if run_healthchecks else []
    if healthcheck_before_phase is None:
        ordered.extend(_healthcheck_command_records(pending_healthchecks))
        pending_healthchecks = []
    for step in command_plan:
        if (
            healthcheck_before_phase is not None
            and pending_healthchecks
            and step.phase == healthcheck_before_phase
        ):
            ordered.extend(_healthcheck_command_records(pending_healthchecks))
            pending_healthchecks = []
        record: dict[str, Any] = {"phase": step.phase, "command": step.command.command}
        if step.database_hook:
            record.update(
                {
                    "database_hook": True,
                    "hook_kind": step.hook_kind,
                    "timeout_seconds": step.command.timeout_seconds,
                }
            )
        ordered.append(record)
    if pending_healthchecks:
        ordered.extend(_healthcheck_command_records(pending_healthchecks))
    if "validate" in phase_names and _should_run_local_coverage(profile):
        coverage_command = profile.validation.coverage.command
        if coverage_command is None:
            raise RuntimeError(
                "_should_run_local_coverage returned True but coverage.command is None"
            )
        coverage_record = {
            "phase": "coverage",
            "command": coverage_command.command,
        }
        if coverage_evidence_status is not None:
            coverage_record["evidence_status"] = coverage_evidence_status
        if coverage_evidence_reason_code is not None:
            coverage_record["evidence_reason_code"] = coverage_evidence_reason_code
        ordered.append(coverage_record)

    records: list[dict[str, Any]] = []
    phase_indices: dict[str, int] = {}
    for item in ordered:
        phase = str(item["phase"])
        phase_indices[phase] = phase_indices.get(phase, 0) + 1
        command_index = phase_indices[phase]
        label = f"{command_index:02d}_{phase}"
        record = dict(item)
        record.update(
            {
                "command_index": command_index,
                "stream_ids": {
                    "stdout": f"validation.{label}.stdout",
                    "stderr": f"validation.{label}.stderr",
                },
            }
        )
        records.append(record)
    return records


def _healthcheck_command_records(healthchecks: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "phase": "healthcheck",
            "command": healthcheck.display_command(),
            "healthcheck_name": healthcheck.name,
            "healthcheck_kind": healthcheck.kind or ("http" if healthcheck.url else "command"),
            "target": healthcheck.target(),
        }
        for healthcheck in healthchecks
    ]


def _validation_tier_for_workspace(workspace: Workspace, profile: WorkspaceProfile) -> int:
    profile_tier = profile.validation.requested_tier
    task_class_tier = 1
    if workspace.task_class == TaskClass.migration_task.value:
        task_class_tier = 3
    elif workspace.task_class in {
        TaskClass.refactor_task.value,
        TaskClass.dependency_task.value,
        TaskClass.build_config_task.value,
    }:
        task_class_tier = 2
    operation_tier = _validate_operation_requested_tier(workspace) or 1
    return max(profile_tier, task_class_tier, operation_tier)


def _validate_operation_requested_tier(workspace: Workspace) -> int | None:
    tiers: list[int] = []
    operations = getattr(workspace, "operations", None) or []
    for operation in operations:
        operation_type = getattr(operation, "type", None)
        if isinstance(operation_type, OperationType):
            operation_type = operation_type.value
        if operation_type != OperationType.validate.value:
            continue

        operation_status = getattr(operation, "status", None)
        if isinstance(operation_status, OperationStatus):
            operation_status = operation_status.value
        metadata: tuple[object, ...]
        if operation_status == OperationStatus.succeeded.value:
            metadata = (
                getattr(operation, "result", None),
                getattr(operation, "payload", None),
            )
        elif operation_status in _RECOVERY_ACTIVE_OPERATION_STATUSES:
            metadata = (getattr(operation, "payload", None),)
        else:
            continue

        operation_tiers = [
            tier
            for tier in (_requested_tier_from_metadata(item) for item in metadata)
            if tier is not None
        ]
        operation_max = max(operation_tiers, default=None)
        if operation_max is not None:
            tiers.append(operation_max)
    return max(tiers, default=None)


def _requested_tier_from_metadata(metadata: object) -> int | None:
    if not isinstance(metadata, Mapping):
        return None
    requested_tier = metadata.get("requested_tier")
    if type(requested_tier) is int and requested_tier > 0:
        return requested_tier
    validation = metadata.get("validation")
    if not isinstance(validation, Mapping):
        return None
    requested_tier = validation.get("requested_tier")
    if type(requested_tier) is int and requested_tier > 0:
        return requested_tier
    return None


def _should_run_local_coverage(profile: WorkspaceProfile) -> bool:
    return (
        profile.validation.strategy.final_gate == "coverage"
        and profile.validation.coverage.command is not None
    )


def _validation_run_reason_code(result: ValidationResult) -> str:
    if result.all_passed:
        return "VALIDATION_OK"
    first_failure = result.first_failure
    if _coverage_has_failing_tests(result.coverage):
        return PYTEST_TEST_FAILURE
    if result.coverage is not None and not result.coverage.ok:
        return result.coverage.reason_code
    if first_failure is None:
        return "VALIDATION_FAILED"
    return first_failure.reason_code


def _apply_baseline_coverage_ratchet(
    result: ValidationResult,
    *,
    baseline_coverage: ValidationCoverageResult | None,
) -> ValidationResult:
    """Accept coverage baseline debt only when a workspace does not regress it.

    AWF's self profile carries an aspirational 99% coverage target. Until the
    existing repo baseline reaches that target, unrelated feature PRs should
    not be forced to repay the whole historical debt. They must, however,
    preserve or improve the measured baseline and must not lower the gate.
    """
    coverage = result.coverage
    if not _coverage_preserves_below_threshold_baseline(
        coverage,
        baseline_coverage=baseline_coverage,
    ):
        return result

    assert coverage is not None  # narrowed by helper above
    command_result = coverage.command_result
    adjusted_command = (
        replace(
            command_result,
            returncode=0,
            reason_code="COVERAGE_BASELINE_DEBT_NO_REGRESSION",
            policy_failed=False,
        )
        if command_result is not None
        else None
    )
    adjusted_coverage = replace(
        coverage,
        status="baseline_debt",
        reason_code="COVERAGE_BASELINE_DEBT_NO_REGRESSION",
        command_result=adjusted_command,
    )
    adjusted_commands = list(result.commands)
    if adjusted_command is not None and command_result is not None:
        adjusted_commands = [
            adjusted_command
            if command.stdout_path == command_result.stdout_path
            and command.stderr_path == command_result.stderr_path
            else command
            for command in result.commands
        ]
    return replace(result, commands=adjusted_commands, coverage=adjusted_coverage)


def _coverage_preserves_below_threshold_baseline(
    coverage: ValidationCoverageResult | None,
    *,
    baseline_coverage: ValidationCoverageResult | None,
) -> bool:
    if coverage is None or baseline_coverage is None:
        return False
    if _coverage_has_failing_tests(coverage):
        return False
    if coverage.reason_code != "COVERAGE_BELOW_THRESHOLD":
        return False
    if coverage.percent is None or baseline_coverage.percent is None:
        return False
    if baseline_coverage.percent >= coverage.minimum_percent:
        return False
    return coverage.percent + 0.005 >= baseline_coverage.percent


def _validation_run_coverage_metadata(
    result: ValidationResult,
    *,
    baseline_coverage: ValidationCoverageResult | None = None,
) -> dict[str, object] | None:
    if result.coverage is None:
        return None
    metadata = result.coverage.as_metadata()
    if baseline_coverage is not None:
        metadata["baseline_percent"] = (
            float(baseline_coverage.percent) if baseline_coverage.percent is not None else None
        )
        metadata["baseline_status"] = baseline_coverage.status
        metadata["baseline_reason_code"] = baseline_coverage.reason_code
    return metadata


def _extract_string_tokens(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str)]


def _coverage_result_from_metadata(metadata: Mapping[str, object]) -> ValidationCoverageResult:
    percent = metadata.get("percent")
    minimum = metadata.get("minimum_percent")
    enforce = metadata.get("enforce")
    gaps = metadata.get("gaps")
    raw_failing_node_ids = metadata.get("failing_test_node_ids")
    raw_failing_evidence = metadata.get("failing_test_evidence")
    raw_provider_failure_evidence = metadata.get("provider_failure_evidence")
    parallel_requested = metadata.get("parallel_workers_requested")
    parallel_effective = metadata.get("parallel_workers_effective")
    parallel_distribution = metadata.get("parallel_distribution")
    return ValidationCoverageResult(
        provider=str(metadata.get("provider") or "python"),
        percent=float(percent) if isinstance(percent, int | float) else None,
        minimum_percent=float(minimum) if isinstance(minimum, int | float) else 0.0,
        enforce=bool(enforce) if isinstance(enforce, bool) else True,
        status=str(metadata.get("status") or "passed"),
        reason_code=str(metadata.get("reason_code") or "COVERAGE_OK"),
        gaps=[item for item in gaps if isinstance(item, dict)] if isinstance(gaps, list) else [],
        failing_test_node_ids=_extract_string_tokens(raw_failing_node_ids),
        failing_test_evidence=_extract_string_tokens(raw_failing_evidence),
        provider_failure_evidence=_extract_string_tokens(raw_provider_failure_evidence),
        parallel_workers_requested=(
            int(parallel_requested) if isinstance(parallel_requested, int) else None
        ),
        parallel_workers_effective=(
            int(parallel_effective) if isinstance(parallel_effective, int) else None
        ),
        parallel_distribution=(
            str(parallel_distribution) if isinstance(parallel_distribution, str) else None
        ),
    )


def _format_coverage_gaps(gaps: list[dict[str, object]]) -> str:
    if not gaps:
        return ""
    top = gaps[:5]
    lines = ["top uncovered areas:"]
    for g in top:
        file_name = g.get("file", "")
        missing = g.get("missing_lines", [])
        missing_cast = missing if isinstance(missing, list) else []
        missing_str = (
            ", ".join(str(m) for m in missing_cast) if missing_cast else "(no missing lines)"
        )
        lines.append(f"  {file_name}: {missing_str}")
    return "\n".join(lines)


def _coverage_has_failing_tests(coverage: ValidationCoverageResult | None) -> bool:
    if coverage is None:
        return False
    return bool(coverage.failing_test_node_ids or coverage.failing_test_evidence)


def _format_failing_test_evidence(coverage: ValidationCoverageResult) -> str:
    node_ids = [str(value) for value in coverage.failing_test_node_ids[:5]]
    evidence = [str(value) for value in coverage.failing_test_evidence[:5]]
    if node_ids and evidence:
        return f"{', '.join(node_ids)}; evidence: {' | '.join(evidence)}"
    return ", ".join(node_ids if node_ids else evidence)


def _coverage_wrapped_pytest_failure_message(
    coverage: ValidationCoverageResult,
) -> str:
    tests = _format_failing_test_evidence(coverage)
    tests_fragment = f": {tests}" if tests else ""
    if coverage.reason_code == "COVERAGE_BELOW_THRESHOLD" and coverage.percent is not None:
        gap_lines = _format_coverage_gaps(coverage.gaps if coverage.gaps else [])
        gap_text = f"\n{gap_lines}" if gap_lines else ""
        return (
            f"validation failed: pytest reported failing tests{tests_fragment}; "
            f"coverage {coverage.percent:.1f}% is also below required "
            f"{coverage.minimum_percent:.1f}%; fix the failing test first, "
            "then address coverage if it remains below threshold"
            f"{gap_text}"
        )
    if coverage.reason_code == "COVERAGE_FAIL_UNDER_NOT_REACHED":
        displayed = (
            f"; displayed rounded coverage was {coverage.percent:.2f}%"
            if coverage.percent is not None
            else ""
        )
        return (
            f"validation failed: pytest reported failing tests{tests_fragment}; "
            "coverage provider also reported that fail-under was not reached"
            f"{displayed}; required coverage is {coverage.minimum_percent:.2f}%"
        )
    if coverage.percent is not None:
        return (
            f"validation failed: pytest reported failing tests{tests_fragment}; "
            f"coverage met the {coverage.minimum_percent:.1f}% requirement "
            f"at {coverage.percent:.1f}%"
        )
    return (
        f"validation failed: pytest reported failing tests{tests_fragment}; "
        "coverage output was not available because the coverage-wrapped test command failed"
    )


def _validation_failure_message(
    result: ValidationResult,
    *,
    baseline_coverage: ValidationCoverageResult | None = None,
) -> str:
    coverage = result.coverage
    first_fail = result.first_failure
    if coverage is not None and _coverage_has_failing_tests(coverage):
        return _coverage_wrapped_pytest_failure_message(coverage)
    if coverage is not None and not coverage.ok:
        baseline_debt = (
            baseline_coverage is not None
            and baseline_coverage.percent is not None
            and baseline_coverage.percent < coverage.minimum_percent
        )
        baseline_suffix = (
            f"; pre-agent base coverage was {baseline_coverage.percent:.1f}%"
            f" against the same {coverage.minimum_percent:.1f}% requirement"
            if baseline_debt
            and baseline_coverage is not None
            and baseline_coverage.percent is not None
            else ""
        )
        if coverage.reason_code == "COVERAGE_BELOW_THRESHOLD" and coverage.percent is not None:
            gap_lines = _format_coverage_gaps(coverage.gaps if coverage.gaps else [])
            gap_text = f"\n{gap_lines}" if gap_lines else ""
            return (
                "validation failed: coverage "
                f"{coverage.percent:.1f}% is below required {coverage.minimum_percent:.1f}%"
                f"{baseline_suffix}; add meaningful tests and do not lower coverage thresholds"
                f"{gap_text}"
            )
        if coverage.reason_code == "COVERAGE_NOT_FOUND":
            return "validation failed: coverage output was not found"
        if coverage.reason_code == "COVERAGE_COMMAND_FAILED":
            return (
                "validation failed: coverage command failed"
                f"{baseline_suffix}; fix the failing tests or add meaningful coverage, "
                "do not lower coverage thresholds"
            )
        if coverage.reason_code == "COVERAGE_FAIL_UNDER_NOT_REACHED":
            displayed = (
                f"; displayed rounded coverage was {coverage.percent:.2f}%"
                if coverage.percent is not None
                else ""
            )
            return (
                "validation failed: coverage provider reported that fail-under was not reached"
                f"{displayed}; required coverage is {coverage.minimum_percent:.2f}%"
                f"{baseline_suffix}; treat provider fail-under output as authoritative "
                "and add meaningful tests instead of relying on rounded coverage"
            )
        if coverage.reason_code == "COVERAGE_PROVIDER_UNSUPPORTED":
            return f"validation failed: unsupported coverage provider {coverage.provider}"

    if first_fail is not None and first_fail.phase == "healthcheck":
        metadata = first_fail.metadata
        name = metadata.get("healthcheck_name")
        kind = metadata.get("healthcheck_kind")
        target = metadata.get("target") or first_fail.command
        attempts = metadata.get("attempts")
        timeout = metadata.get("timeout_seconds")
        stream_ids = first_fail.stream_ids
        stdout_stream = stream_ids.get("stdout")
        stderr_stream = stream_ids.get("stderr")
        log_hint = ", ".join(
            stream for stream in (stdout_stream, stderr_stream) if isinstance(stream, str)
        )
        details = (
            f"validation failed: health check {name if isinstance(name, str) else first_fail.command}"
            f" ({kind if isinstance(kind, str) else 'unknown'} target={target}) "
            f"failed with {first_fail.reason_code}"
        )
        if isinstance(timeout, int | float):
            details += f" after {_format_duration_for_message(timeout)}s"
        if isinstance(attempts, int):
            details += f" across {attempts} attempt(s)"
        if log_hint:
            details += f"; logs: {log_hint}"
        return details
    return f"validation failed: {first_fail.command}" if first_fail else "validation failed"


def _post_validation_conformance_fix_result(
    *,
    failure: _PlanningRunFailure,
    workspace_id: str,
    artifacts_root: Path,
    attempt: int | None = None,
) -> ValidationResult:
    artifacts_dir = artifacts_root / workspace_id / "post_validation_conformance"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    attempt_value: object = attempt
    if attempt_value is None and failure.details:
        attempt_value = failure.details.get("attempt")
    suffix = (
        f".{attempt_value}"
        if isinstance(attempt_value, int)
        and not isinstance(attempt_value, bool)
        and attempt_value > 0
        else ""
    )
    stdout_path = artifacts_dir / f"post_validation_conformance{suffix}.stdout"
    stderr_path = artifacts_dir / f"post_validation_conformance{suffix}.stderr"
    stdout_path.write_text(_post_validation_conformance_failure_text(failure), encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")
    return ValidationResult(
        commands=[
            ValidationCommandResult(
                command="post-validation plan conformance",
                returncode=1,
                duration_seconds=0.0,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                phase="conformance",
                reason_code=failure.reason_code or PLAN_CONFORMANCE_UNSATISFIED,
                policy_failed=True,
            )
        ]
    )
