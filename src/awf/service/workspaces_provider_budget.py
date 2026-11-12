"""``WorkspaceService`` wiring for the managed provider-budget control (MCP surface)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from awf.api.schemas import WorkspaceControlResponse
from awf.common.config import get_settings
from awf.service.openrouter_management import management_client_from_settings


class _WorkspaceProviderBudgetMixin:
    """Mirrors ``POST /v1/workspaces/{id}/provider-budget`` for in-process clients."""

    async def set_provider_budget(
        self: Any,
        workspace_id: str,
        *,
        usd: float | Decimal | str,
        reason: str | None = None,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> WorkspaceControlResponse:
        """Set the managed OpenRouter budget (USD) of a workspace and resume a park."""
        settings = self._settings or get_settings()
        async with self._factory() as session:
            result: WorkspaceControlResponse = await self._controls(session).set_provider_budget(
                workspace_id,
                usd=usd,
                reason=reason,
                idempotency_key=idempotency_key,
                expected_version=expected_version,
                management_client=management_client_from_settings(settings),
            )
            await session.commit()
            return result
