"""Deferred Cursor Auto Router preflight after provision-time profile resolution.

Extracted from ``awf.node.provisioner`` so that module stays under the
first-party line-count guardrail. Completes the adoption deferral in
``pr_monitor_adoption_cursor_preflight`` once the checkout profile is known.
"""

from __future__ import annotations

from typing import Any

from awf.common.logging import get_logger
from awf.db.enums import FailureReason, WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import WorkspaceRepository
from awf.profiles.models import WorkspaceProfile
from awf.service.pr_monitor_adoption_cursor_preflight import (
    run_deferred_cursor_auto_mode_provider_preflight,
)
from awf.service.workspaces_create import _record_provider_readiness_preflight

_log = get_logger(__name__)


class ProvisionerCursorPreflightMixin:
    """Mixin: run adoption-deferred Cursor Router preflight after profile resolve."""

    async def _run_deferred_cursor_auto_router_preflight(
        self: Any,
        *,
        workspace_id: str,
        ws: Workspace,
        profile: WorkspaceProfile,
        execution_claim_epoch: int | None = None,
    ) -> bool:
        """Return True when provisioning must stop (Router preflight blocked)."""

        task_policy = ws.task_policy if isinstance(ws.task_policy, dict) else None
        preflight = await run_deferred_cursor_auto_mode_provider_preflight(
            agent=ws.agent,
            task_policy=task_policy,
            resolved_profile=profile.model_dump(mode="json", by_alias=True),
        )
        if preflight is None:
            # Defense in depth: if a prior attempt committed a blocking snapshot
            # but deferred probe returned None (e.g. needs-check bypassed), do
            # not treat that as a passed gate and continue into stack launch.
            existing = (
                task_policy.get("provider_readiness_preflight")
                if isinstance(task_policy, dict)
                else None
            )
            if isinstance(existing, dict) and existing.get("blocks_launch") is True:
                reason_code = str(
                    existing.get("reason_code") or "PROVIDER_READINESS_PRECHECK_FAILED"
                )
                message = str(
                    existing.get("message") or "Provider readiness blocked workspace launch."
                )
                await self._mark_failed(
                    workspace_id=workspace_id,
                    failure_reason=FailureReason.policy_failure,
                    message=message[:2000],
                    from_status=WorkspaceStatus.provisioning,
                    reason_code=reason_code,
                    event_payload={"provider_readiness_preflight": dict(existing)},
                    execution_claim_epoch=execution_claim_epoch,
                )
                return True
            return False
        if preflight.get("blocks_launch") is True:
            reason_code = str(preflight.get("reason_code") or "PROVIDER_READINESS_PRECHECK_FAILED")
            message = str(
                preflight.get("message") or "Provider readiness blocked workspace launch."
            )
            _log.error(
                "provisioner.deferred_cursor_router_preflight_blocked",
                workspace_id=workspace_id,
                reason_code=reason_code,
            )
            # Persist the checkout-resolved profile and blocking readiness
            # snapshot before failing so GET/overview can surface the structured
            # preflight (via task_policy) and a normal retry inherits
            # profile-only credentials (e.g. CURSOR_API_KEY) via
            # source.resolved_profile instead of re-probing with an empty env.
            resolved_profile_dict = profile.model_dump(mode="json", by_alias=True)
            async with self._session_factory() as session:
                repo = WorkspaceRepository(session)
                persisted = await repo.get_for_update(workspace_id)
                if (
                    persisted is not None
                    and persisted.status == WorkspaceStatus.provisioning.value
                    and (
                        execution_claim_epoch is None
                        or persisted.execution_claim_epoch == execution_claim_epoch
                    )
                ):
                    if persisted.resolved_profile is None:
                        persisted.resolved_profile = resolved_profile_dict
                        ws.resolved_profile = resolved_profile_dict
                    policy = dict(persisted.task_policy or {})
                    policy["provider_readiness_preflight"] = dict(preflight)
                    persisted.task_policy = policy
                    ws.task_policy = policy
                await session.commit()
            await self._mark_failed(
                workspace_id=workspace_id,
                failure_reason=FailureReason.policy_failure,
                message=message[:2000],
                from_status=WorkspaceStatus.provisioning,
                reason_code=reason_code,
                event_payload={"provider_readiness_preflight": dict(preflight)},
                execution_claim_epoch=execution_claim_epoch,
            )
            return True

        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            # Row lock makes status/epoch fence atomic with the readiness write
            # so cancel or a later claimant cannot commit between read and commit.
            persisted = await repo.get_for_update(workspace_id)
            if persisted is None:
                # Cancel/destroy may remove the row during the Router probe;
                # do not continue into egress/stack launch with only in-memory ws.
                return True
            if persisted.status != WorkspaceStatus.provisioning.value:
                await self._record_stale_action_skip(
                    repo,
                    persisted,
                    action="deferred_cursor_router_preflight",
                    expected=WorkspaceStatus.provisioning,
                    reason_code="PROVISIONER_STALE_STATUS",
                )
                await session.commit()
                return True
            # Status alone is insufficient: a later claimant can advance
            # execution_claim_epoch while the row stays ``provisioning``. Fence
            # the readiness snapshot/event write the same way the blocking
            # branch and egress-audit path do so a reclaimed provision cannot
            # mutate the new claimant's timeline.
            if (
                execution_claim_epoch is not None
                and persisted.execution_claim_epoch != execution_claim_epoch
            ):
                _log.info(
                    "provisioner.skip_fenced_epoch",
                    workspace_id=workspace_id,
                    action="deferred_cursor_router_preflight",
                    expected_epoch=execution_claim_epoch,
                    actual_epoch=persisted.execution_claim_epoch,
                )
                return True
            # Persist checkout-resolved profile before continuing so a later
            # pre-launch failure (e.g. companion host-port checks) still leaves
            # profile-only credentials (CURSOR_API_KEY) on the row for retry.
            # The blocking branch already persists for the same reason.
            if persisted.resolved_profile is None:
                resolved_profile_dict = profile.model_dump(mode="json", by_alias=True)
                persisted.resolved_profile = resolved_profile_dict
                ws.resolved_profile = resolved_profile_dict
            policy = dict(persisted.task_policy or {})
            policy["provider_readiness_preflight"] = dict(preflight)
            persisted.task_policy = policy
            ws.task_policy = policy
            await _record_provider_readiness_preflight(repo, persisted, preflight)
            await session.commit()
        return False
