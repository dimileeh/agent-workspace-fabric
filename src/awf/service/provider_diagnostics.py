"""Isolated provider candidate workspaces using normal durable AWF ownership."""

from __future__ import annotations

import os
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from awf.api.schemas_providers import (
    ProviderConnectionTestRequest,
    ProviderConnectionTestResponse,
    ProviderQueryRequest,
)
from awf.common.agent_execution import (
    MANAGED_PROVIDER_KEY_WORKER_VERSION,
    ExecutionSelection,
    resolve_execution,
)
from awf.common.config import Settings
from awf.profiles.models import WorkspaceProfile
from awf.profiles.resolver import resolve_workspace_profile
from awf.runtime.provider_diagnostic_fixture import FIXTURE_VERSION, PROMPT
from awf.service.controls_errors import WorkspaceControlError
from awf.service.disk import DiskCheck
from awf.service.workspaces_retry_errors import WorkspaceRetryError

DIAGNOSTIC_KIND = "provider_connection_test"
DIAGNOSTIC_KEY = "provider_diagnostic"
DIAGNOSTIC_WORKER_VERSION = 2
INTERNAL_REPO = "awf-internal://provider-diagnostic/v1"


class DiagnosticReplayError(WorkspaceControlError, WorkspaceRetryError):
    """Shared control/retry error, understood by both existing client adapters."""


def is_diagnostic(policy: object) -> bool:
    return isinstance(policy, Mapping) and isinstance(policy.get(DIAGNOSTIC_KEY), Mapping)


def query_material(
    request: ProviderQueryRequest, *, environ: Mapping[str, str], managed_openrouter: bool = False
) -> tuple[WorkspaceProfile, ExecutionSelection]:
    profile = resolve_workspace_profile(
        worktree_path=None, inline_profile=request.profile, profile_ref=request.profile_ref
    ).profile
    selection = resolve_execution(
        agent=str(request.agent),
        provider=request.provider,
        model=request.model
        if isinstance(request, ProviderConnectionTestRequest)
        else "catalog-candidate",
        effort=request.effort if isinstance(request, ProviderConnectionTestRequest) else None,
        allow_unverified=True,
        profile=profile.model_dump(mode="json"),
        environ=environ,
        managed_openrouter=managed_openrouter,
    )
    return profile, selection


def diagnostic_material(
    request: ProviderConnectionTestRequest,
    *,
    environ: Mapping[str, str],
    managed_openrouter: bool = False,
) -> tuple[dict[str, Any], dict[str, Any], ExecutionSelection]:
    profile, selected = query_material(
        request, environ=environ, managed_openrouter=managed_openrouter
    )
    # Project services/volumes cannot be borrowed. Self-contained model sidecars
    # can be reproduced; dependencies, mounts, ports, builds and auth cannot.
    from awf.service.provider_readiness import is_secret_env_key

    services = []
    for service in profile.services:
        if service.name != selected.network_service:
            continue
        if (
            service.build_context
            or service.env_file
            or service.depends_on
            or service.volumes
            or service.ports
            or service.privileged
            # Compose also expands bare $NAME references in these sidecar values.
            or any(
                re.search(r"\$(?:\{|[A-Za-z_])", value or "")
                for value in (
                    service.image,
                    service.command,
                    service.healthcheck_cmd,
                    *service.environment.values(),
                )
            )
            or any(is_secret_env_key(k) for k in service.environment)
        ):
            raise ValueError("PROVIDER_DIAGNOSTIC_PROFILE_UNSUPPORTED")
        services.append(service.model_dump(mode="json"))
    environment = {}
    # A managed key is minted per invocation and passed at exec time only.
    if selected.third_party and selected.provider == "openrouter" and not selected.managed_key:
        environment["OPENROUTER_API_KEY"] = (
            "${" + str(selected.credential_ref).removeprefix("env/") + "}"
        )
    # Native diagnostics retain only the selected native model-service slots.
    if not selected.third_party:
        names = (
            ("OPENAI_API_KEY", "CODEX_API_KEY")
            if selected.agent == "codex"
            else ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")
        )
        environment = {
            k: profile.runtime.environment.get(k, "${" + k + "}")
            for k in names
            if k in profile.runtime.environment or environ.get(k)
        }
        for secret in profile.secrets:
            if secret.target not in names:
                continue
            source = (secret.ref or "").removeprefix("env/")
            if (
                secret.kind != "env"
                or secret.provider != "env"
                or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", source)
            ):
                raise ValueError("PROVIDER_DIAGNOSTIC_PROFILE_UNSUPPORTED")
            environment[secret.target] = "${" + source + "}"
    if any(
        not re.fullmatch(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}", value) for value in environment.values()
    ):
        raise ValueError("PROVIDER_CREDENTIAL_REFERENCE_REQUIRED")
    safe = WorkspaceProfile.model_validate(
        {
            "name": "provider-diagnostic",
            "docker": {"mode": "none"},
            "runtime": {"environment": environment},
            "services": services,
            "security": {
                "egress": {
                    "mode": "open",
                    "open_explanation": "Selected provider diagnostic connection",
                }
            },
        }
    ).model_dump(mode="json")
    spec = {
        "version": 1,
        "fixture_version": FIXTURE_VERSION,
        "deadline_seconds": request.deadline_seconds,
        "selection": selected.model_dump(mode="json"),
        "profile_ref": request.profile_ref,
        "profile_identity": {
            "name": profile.name,
            "version": profile.version,
            "source": profile.source,
        },
        "profile": safe,
        "state": "pending",
    }
    return spec, safe, selected


def reject_diagnostic_control(policy: object, action: str) -> None:
    if is_diagnostic(policy) and action not in {"cancel", "stop", "destroy"}:
        raise DiagnosticReplayError(
            error_code="PROVIDER_DIAGNOSTIC_REPLAY_FORBIDDEN",
            message="Diagnostics cannot be replayed; submit a new connection test.",
        )


async def create_connection_test(
    service: Any,
    request: ProviderConnectionTestRequest,
    *,
    idempotency_key: str,
    disk_check: DiskCheck | None = None,
    admission_check: Callable[[], Awaitable[None]] | None = None,
) -> ProviderConnectionTestResponse:
    from datetime import UTC, datetime

    from awf.api.schemas import WorkspaceCreateRequest
    from awf.common.config import get_settings
    from awf.db.enums import OperationStatus, OperationType
    from awf.db.repositories import (
        OperationRepository,
        WorkerHeartbeatRepository,
        WorkspaceRepository,
    )
    from awf.service.workspaces import WorkspaceCreateIdempotencyConflictError
    from awf.service.workspaces_create import create_workspace_row_checked

    settings: Settings = service._settings or get_settings()
    spec, safe, selected = diagnostic_material(
        request,
        environ=os.environ,
        managed_openrouter=getattr(settings, "openrouter_management_key", None) is not None,
    )
    spec["runtime_image"] = settings.agent_runtime_image
    if settings.hosted_node is not None:
        raise ValueError("PROVIDER_DIAGNOSTIC_HOSTED_UNSUPPORTED")
    payload = WorkspaceCreateRequest.model_validate(
        {
            "repo": {"url": INTERNAL_REPO, "base_branch": "fixture"},
            "task": {
                "title": "Provider connection diagnostic",
                "prompt": PROMPT,
                "agent": selected.agent,
                "provider": selected.provider,
                "model": selected.model,
                "effort": request.effort,
            },
            "workspace": {"profile": safe},
            "preflight": {"allow_unverified_model": True},
        }
    )
    async with service._factory() as session:
        repo = WorkspaceRepository(session)
        await repo.acquire_idempotency_key_lock(idempotency_key)
        existing = await repo.get_by_idempotency_key(idempotency_key)
        if existing is not None:
            stored = (existing.task_policy or {}).get(DIAGNOSTIC_KEY, {})
            if stored.get("request") != spec:
                raise WorkspaceCreateIdempotencyConflictError()
            operation_id = stored.get("operation_id")
            if not isinstance(operation_id, str):
                raise ValueError("PROVIDER_DIAGNOSTIC_CONTRACT_UNSUPPORTED")
            return ProviderConnectionTestResponse(
                workspace_id=existing.id, operation_id=operation_id, status=existing.status
            )
        if admission_check is not None:
            await admission_check()
        if disk_check is not None and not disk_check.ok:
            from awf.service.workspaces import WorkspaceCreateInsufficientDiskError

            raise WorkspaceCreateInsufficientDiskError(disk_check)
        if not await WorkerHeartbeatRepository(session).supports_execution_contract(
            now=datetime.now(UTC),
            minimum_version=MANAGED_PROVIDER_KEY_WORKER_VERSION
            if selected.managed_key
            else DIAGNOSTIC_WORKER_VERSION,
        ):
            raise ValueError("PROVIDER_DIAGNOSTIC_WORKER_UNSUPPORTED")
        ws = await create_workspace_row_checked(
            session,
            repo,
            payload,
            settings=settings,
            idempotency_key=idempotency_key,
            diagnostic_spec=spec,
            disk_check=disk_check,
        )
        operation = await OperationRepository(session).create(
            workspace_id=ws.id,
            operation_type=OperationType.start,
            status=OperationStatus.pending,
            payload={"source": DIAGNOSTIC_KIND},
        )
        ws.task_policy = {
            **ws.task_policy,
            DIAGNOSTIC_KEY: {
                **ws.task_policy[DIAGNOSTIC_KEY],
                "operation_id": operation.id,
            },
        }
        await session.commit()
        return ProviderConnectionTestResponse(
            workspace_id=ws.id, operation_id=operation.id, status=ws.status
        )


def provider_error(code: str) -> dict[str, Any]:
    from awf.api.schemas import ErrorResponse

    messages = {
        "PROVIDER_SELECTION_INVALID": "Invalid provider selection or profile.",
        "IDEMPOTENCY_KEY_REQUIRED": "Supply an Idempotency-Key.",
        "IDEMPOTENCY_CONFLICT": "Key already used for a different diagnostic.",
        "PROVIDER_READINESS_PRECHECK_FAILED": "Compatible worker capacity is required.",
    }
    return ErrorResponse(
        error_code=code, message=messages.get(code, "Provider diagnostic could not be admitted.")
    ).model_dump(mode="json")


def require_diagnostic_contract(workspace: Any) -> None:
    from awf.common.agent_execution import execution_from_policy

    diagnostic = (workspace.task_policy or {}).get(DIAGNOSTIC_KEY, {})
    request = diagnostic.get("request", {})
    selected = execution_from_policy(workspace.task_policy)
    requested_selection = request.get("selection")
    if (
        selected is not None
        and not selected.third_party
        and not selected.effort_explicit
        and isinstance(requested_selection, dict)
    ):
        # The foundation freezes configured native defaults once at first launch.
        requested_selection = {**requested_selection, "effort": selected.effort}
    if (
        workspace.task_kind != DIAGNOSTIC_KIND
        or request.get("version") != 1
        or request.get("fixture_version") != FIXTURE_VERSION
        or selected is None
        or requested_selection != selected.model_dump(mode="json")
        or type(request.get("deadline_seconds")) is not int
        or not 1 <= request["deadline_seconds"] <= 900
    ):
        raise ValueError("PROVIDER_DIAGNOSTIC_CONTRACT_UNSUPPORTED")


def record_diagnostic_cancellation(workspace: Any) -> None:
    if workspace.status not in {"cancelled", "destroyed"} or not is_diagnostic(
        workspace.task_policy
    ):
        return
    diagnostic = dict(workspace.task_policy[DIAGNOSTIC_KEY])
    if diagnostic.get("result"):
        return
    diagnostic.update(
        state="cancelled",
        result={
            "outcome": "cancelled",
            "reason_code": "WORKSPACE_CANCELLED",
            "protocol_evidence": "inconclusive",
            "fixture_verification": False,
        },
    )
    workspace.task_policy = {**workspace.task_policy, DIAGNOSTIC_KEY: diagnostic}


async def settle_quiescent_diagnostic(session: Any, workspace: Any) -> None:
    """Caller has proved runtime quiescence through successful resource teardown."""
    if not is_diagnostic(workspace.task_policy):
        return
    from awf.db.enums import OperationStatus, OperationType
    from awf.db.repositories import OperationRepository

    record_diagnostic_cancellation(workspace)
    result = workspace.task_policy[DIAGNOSTIC_KEY].get("result") or {
        "outcome": "inconclusive",
        "reason_code": "PROVIDER_DIAGNOSTIC_INTERRUPTED",
    }
    status = (
        OperationStatus.cancelled
        if workspace.status in {"cancelled", "destroyed"}
        else OperationStatus.succeeded
        if workspace.status == "completed" and result.get("outcome") == "passed"
        else OperationStatus.failed
    )
    for operation in await OperationRepository(session).list_for_workspace(
        workspace.id, operation_type=OperationType.start
    ):
        if (operation.payload or {}).get("source") == DIAGNOSTIC_KIND and operation.status in {
            "pending",
            "running",
        }:
            await OperationRepository(session).finish(operation, status=status, result=result)
