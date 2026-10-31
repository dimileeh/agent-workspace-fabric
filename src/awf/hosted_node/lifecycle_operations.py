"""Authority joins for ordinary hosted workspace lifecycle services."""

import asyncio
import os
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime

from pydantic import ValidationError
from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from awf.common.commands import AsyncioSubprocessRunner, CommandResult
from awf.common.config import Settings
from awf.common.github_client import RepoRef
from awf.common.redaction import redact_secrets
from awf.db.models import TaskAttempt, Workspace
from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.lifecycle_store import locked
from awf.hosted_node.models import Receipt
from awf.hosted_node.runtime_authority import SESSION_KEY, WorkspaceAuthority
from awf.hosted_node.runtime_schemas import (
    POLICY_KEY,
    HostedControlAuthorization,
    HostedReauthorization,
    HostedWorkspace,
)
from awf.hosted_node.schemas import Binding


def authority_for(session: AsyncSession, supplied: object = None) -> WorkspaceAuthority | None:
    authority = session.info.get(SESSION_KEY)
    if isinstance(authority, WorkspaceAuthority):
        return authority
    if supplied is not None:
        raise MaterialDeniedError("HOSTED_RUNTIME_INTEGRATION_REQUIRED", 403)
    return None


async def commit_hosted(session: AsyncSession) -> None:
    if authority_for(session) is not None:
        expiry = session.info.get("awf_hosted_lifecycle_expiry")
        if expiry is not None and expiry <= datetime.now(UTC):
            raise MaterialDeniedError("PROTECTED_MATERIAL_UNAVAILABLE", 503)
        # The response is a durable acknowledgement, not a request-scope promise.
        await session.commit()


def check_source(
    authority: WorkspaceAuthority,
    row: Workspace | None,
    namespace: str,
    *,
    historical: bool = False,
) -> HostedWorkspace:
    if row is None or not (row.task_policy or {}).get(POLICY_KEY):
        raise MaterialDeniedError("HOSTED_WORKSPACE_REQUIRED", 403)
    if (row.task_policy or {}).get("hosted_recovery", {}).get("disposition") == "attention":
        raise MaterialDeniedError("RECOVERY_ATTENTION_REQUIRED", 409)
    try:
        association = HostedWorkspace.model_validate(row.task_policy[POLICY_KEY])
    except ValidationError:
        raise MaterialDeniedError("HOSTED_WORKSPACE_BINDING_MISMATCH", 403) from None
    previous = association.credentials.agent.identity
    current = authority.receiver.identity
    identity_matches = previous == current or (
        historical
        and previous.owner == current.owner
        and previous.boot_epoch < current.boot_epoch
        and (
            previous.recovery_generation < current.recovery_generation
            or (
                previous.recovery_generation == current.recovery_generation
                and previous.incarnation == current.incarnation
            )
        )
    )
    if (
        association.workspace_id != row.id
        or association.credentials.agent.namespace != namespace
        or not identity_matches
        or row.node_id not in (None, authority.receiver.identity.owner)
    ):
        raise MaterialDeniedError("HOSTED_WORKSPACE_BINDING_MISMATCH", 403)
    return association


async def lock_materials(
    session: AsyncSession,
    authority: WorkspaceAuthority,
    association: HostedWorkspace,
    *,
    new: bool = False,
) -> None:
    """Serialize the final receipt/identity check with revocation and lifecycle writes."""
    gate = await locked(session, authority.receiver.identity)
    if gate.phase != "ready" or gate.recovery_key is None or gate.restore_quarantine is not None:
        raise MaterialDeniedError("NODE_ADMISSION_CLOSED", 409)
    if new:
        await authority.guard_new_workspace(session)
    receiver = authority.receiver
    for anchor in (association.credentials.agent, association.credentials.github):
        receiver.expected(anchor)
        prep = await receiver.preparation(session, anchor)
        assert prep is not None
        grant = await receiver.grant(session, prep, anchor)
        receipt = await session.get(Receipt, grant.current) if grant and grant.current else None
        if (
            grant is None
            or grant.revoked
            or receipt is None
            or receipt.state != "installed"
            or await receiver.superseded(session, grant)
        ):
            raise MaterialDeniedError("PROTECTED_MATERIAL_UNAVAILABLE", 503)
        current = Binding.model_validate(receipt.binding)
        refreshed = {"revision", "fence", "expires_at", "idempotency_key"}
        if (
            anchor.model_dump(exclude=refreshed) != current.model_dump(exclude=refreshed)
            or current.revision < anchor.revision
            or current.fence < anchor.fence
        ):
            raise MaterialDeniedError("HOSTED_WORKSPACE_BINDING_MISMATCH", 403)
        if current.expires_at <= datetime.now(UTC) or not await asyncio.to_thread(
            receiver.store.exists, prep.id, receipt.id
        ):
            raise MaterialDeniedError("PROTECTED_MATERIAL_UNAVAILABLE", 503)
        session.info["awf_hosted_lifecycle_expiry"] = min(
            session.info.get("awf_hosted_lifecycle_expiry", current.expires_at),
            current.expires_at,
        )


async def new_destination(
    session: AsyncSession, authority: WorkspaceAuthority, association: HostedWorkspace
) -> None:
    await lock_materials(session, authority, association, new=True)
    await session.execute(
        text(
            "SELECT pg_advisory_xact_lock(hashtext('awf:hosted-workspace'), hashtext(:workspace_id))"
        ),
        {"workspace_id": association.workspace_id},
    )
    if await session.get(Workspace, association.workspace_id) is not None:
        raise MaterialDeniedError("HOSTED_WORKSPACE_ID_CONFLICT", 409)


async def require_source_quiet(session: AsyncSession, source: Workspace) -> None:
    from awf.service.workspaces_retry_runtime import _source_runtime_not_yet_released

    if (
        source.status not in {"failed", "cancelled", "completed", "destroyed"}
        or source.execution_claimed_by is not None
        or source.monitor_claimed_by is not None
        or await _source_runtime_not_yet_released(session, source)
    ):
        raise MaterialDeniedError("HOSTED_SOURCE_NOT_QUIESCENT", 409)


async def reject_pr_attention(session: AsyncSession, repo: RepoRef, pr_number: int) -> None:
    # Run under the existing node mutex. Query only held rows for this PR number,
    # not historical event/attempt inventories or full workspace objects.
    urls = await session.stream_scalars(
        select(Workspace.repo_url)
        .where(
            Workspace.pr_number == pr_number,
            Workspace.task_policy["hosted_recovery"]["disposition"].as_string() == "attention",
        )
        .execution_options(yield_per=64)
    )
    try:
        async for url in urls:
            previous = RepoRef.from_url(url)
            if previous.forge == repo.forge and previous.slug().lower() == repo.slug().lower():
                raise MaterialDeniedError("RECOVERY_ATTENTION_REQUIRED", 409)
    finally:
        await urls.close()


async def check_task_join(
    session: AsyncSession, task_id: str, association: HostedWorkspace
) -> None:
    # The ordinary task repository holds the task row lock. Historical attempts
    # are ownership evidence only, never current runtime/credential authority.
    stored = Workspace.task_policy[POLICY_KEY]["credentials"]["agent"]
    mismatch = await session.scalar(
        select(Workspace.id)
        .join(TaskAttempt, TaskAttempt.workspace_id == Workspace.id)
        .where(
            TaskAttempt.task_id == task_id,
            or_(
                stored["namespace"]
                .as_string()
                .is_distinct_from(association.credentials.agent.namespace),
                stored["identity"]["owner"]
                .as_string()
                .is_distinct_from(association.credentials.agent.identity.owner),
                Workspace.task_policy["hosted_recovery"]["disposition"].as_string() == "attention",
            ),
        )
        .limit(1)
    )
    if mismatch is not None:
        raise MaterialDeniedError("HOSTED_TASK_BINDING_MISMATCH", 403)


async def control_authority(
    session: AsyncSession,
    workspace_id: str,
    authorization: HostedControlAuthorization | None,
    *,
    reauthorization: HostedReauthorization | None = None,
    grants: list[str] | None = None,
    operator: str | None = None,
) -> None:
    authority = authority_for(session, authorization or reauthorization)
    if authority is None:
        row = await session.get(Workspace, workspace_id)
        if (
            row is not None
            and isinstance(row.task_policy, dict)
            and row.task_policy.get(POLICY_KEY)
        ):
            raise MaterialDeniedError("HOSTED_RUNTIME_INTEGRATION_REQUIRED", 403)
        return
    if authorization is None:
        raise MaterialDeniedError("HOSTED_CONTROL_AUTHORIZATION_REQUIRED", 403)
    if grants and (not authorization.allow_grants or operator != authorization.actor):
        raise MaterialDeniedError("HOSTED_GUIDE_GRANT_AUTHORIZATION_REQUIRED", 403)
    if reauthorization is not None:
        from awf.hosted_node.lifecycle_reauthorization import authorize_rebinding

        await authorize_rebinding(session, authority, workspace_id, authorization, reauthorization)
        return
    row = await session.get(Workspace, workspace_id)
    association = check_source(authority, row, authorization.namespace)
    assert row is not None
    await authority.materials(association, row.agent, current=True)
    await lock_materials(session, authority, association)
    await session.refresh(row, with_for_update=True)
    if check_source(authority, row, authorization.namespace) != association:
        raise MaterialDeniedError("HOSTED_WORKSPACE_BINDING_MISMATCH", 403)


def admission_environment(
    authority: WorkspaceAuthority, settings: Settings, values: dict[str, str]
) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(authority.receiver.config.material_root / "no-host-auth"),
        "LANG": "C.UTF-8",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "DOCKER_HOST": settings.docker_host,
        **values,
    }


class AdmissionCommandRunner(AsyncioSubprocessRunner):
    """Read-only forge admission with fresh material, never inherited host auth."""

    def __init__(self, environment: Mapping[str, str]):
        self.environment = dict(environment)

    async def run(
        self,
        args: list[str],
        *,
        input_bytes: bytes | None = None,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        del env  # Nested caller settings must never replace the authorized environment.
        result = await super().run(
            args,
            input_bytes=input_bytes,
            cwd=cwd,
            env=self.environment,
            timeout_seconds=timeout_seconds,
        )
        secrets = tuple(
            self.environment[key]
            for key in ("GH_TOKEN", "GITHUB_TOKEN", "CODEX_API_KEY", "ANTHROPIC_API_KEY")
            if key in self.environment
        )
        return replace(
            result,
            stdout=redact_secrets(result.stdout, extra_secrets=secrets),
            stderr=redact_secrets(result.stderr, extra_secrets=secrets),
            stdout_bytes=None,
        )
