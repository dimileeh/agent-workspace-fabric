"""Deferred Cursor Auto Router preflight after provision-time profile resolution.

Extracted from ``awf.node.provisioner`` so that module stays under the
first-party line-count guardrail. Completes the adoption deferral in
``pr_monitor_adoption_cursor_preflight`` once the checkout profile is known.
"""

from __future__ import annotations

from typing import Any

from awf.common.audit import redact_audit_value
from awf.common.logging import get_logger
from awf.db.enums import FailureReason, WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import WorkspaceRepository
from awf.node.provisioner_helpers import (
    _stamp_trusted_base_provenance_for_persisted_profile,
)
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
        trusted_base_profile_sha: str | None = None,
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
                    trusted_base_profile_sha=trusted_base_profile_sha,
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
            # Persist a secret-safe checkout-profile snapshot with the blocking
            # readiness result. The retry resolver reacquires provider
            # credentials through declared secret sources instead of storing
            # raw values in Workspace.resolved_profile.
            # When this attempt resolved from the trusted base, replace any
            # legacy freeze with that snapshot and stamp provenance in the same
            # transaction so retry cannot inherit an unstamped trusted freeze.
            resolved_profile_dict = redact_audit_value(
                profile.model_dump(mode="json", by_alias=True)
            )
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
                    published_resolved_profile = False
                    if persisted.resolved_profile is None or trusted_base_profile_sha is not None:
                        persisted.resolved_profile = resolved_profile_dict
                        ws.resolved_profile = resolved_profile_dict
                        published_resolved_profile = True
                    _stamp_trusted_base_provenance_for_persisted_profile(
                        persisted,
                        trusted_base_sha=trusted_base_profile_sha,
                        published_resolved_profile=published_resolved_profile,
                    )
                    ws.task_policy = persisted.task_policy
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
                trusted_base_profile_sha=trusted_base_profile_sha,
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
            # Do NOT publish resolved_profile on the ready path. Host-port
            # admission (_check_auto_resolved_profile_host_ports) must commit
            # that claim under the advisory lock so concurrent auto-profile
            # provisioners serialize first-committer-wins. Publishing ports
            # here lets two Cursor Auto workspaces both become visible before
            # either lock, and both then fail the later conflict check.
            # The blocking branch still publishes so a Router-blocked failure
            # leaves profile-only credentials (e.g. CURSOR_API_KEY) for retry.
            # Later pre-launch failures (companion / auto-profile host-port
            # checks, LocalEgressPolicyError, companion ProfileResolutionError)
            # persist the snapshot via _mark_failed for the same
            # retry-credential reason without reopening the port race.
            policy = dict(persisted.task_policy or {})
            policy["provider_readiness_preflight"] = dict(preflight)
            persisted.task_policy = policy
            ws.task_policy = policy
            await _record_provider_readiness_preflight(repo, persisted, preflight)
            await session.commit()
        return False
