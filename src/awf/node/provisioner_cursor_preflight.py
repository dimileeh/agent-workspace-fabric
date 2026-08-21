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
            persisted = await repo.get(workspace_id)
            if persisted is None:
                return False
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
            policy = dict(persisted.task_policy or {})
            policy["provider_readiness_preflight"] = dict(preflight)
            persisted.task_policy = policy
            ws.task_policy = policy
            await _record_provider_readiness_preflight(repo, persisted, preflight)
            await session.commit()
        return False
