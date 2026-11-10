"""Canonical provider discovery and one-shot diagnostic MCP tools."""

from __future__ import annotations

import os
from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import ValidationError

from awf.api.schemas_providers import ProviderConnectionTestRequest, ProviderQueryRequest
from awf.profiles.resolver import ProfileResolutionError
from awf.service.provider_catalog import ProviderCatalog
from awf.service.provider_diagnostics import create_connection_test, provider_error, query_material
from awf.service.workspaces import (
    WorkspaceCreateIdempotencyConflictError,
    WorkspaceProviderReadinessBlockedError,
)


def register_provider_tools(*, mcp: FastMCP, service: Any, safe_result: Any) -> None:
    catalog = ProviderCatalog()

    @mcp.tool()
    async def awf_query_provider_models(
        agent: str,
        provider: str,
        profile_ref: str | None = None,
        profile: dict[str, Any] | None = None,
    ) -> Any:
        """List selected-provider catalog claims; manual entry remains possible."""
        try:
            request = ProviderQueryRequest.model_validate(
                {
                    "agent": agent,
                    "provider": provider,
                    "profile_ref": profile_ref,
                    "profile": profile,
                }
            )
            resolved, selected = query_material(request, environ=os.environ)
            result = await catalog.query(
                selected, environ=os.environ, context=str(profile_ref or "") + ":" + resolved.name
            )
            return safe_result(result.model_dump(mode="json"))
        except (ValidationError, ValueError, ProfileResolutionError):
            return safe_result(
                provider_error("PROVIDER_SELECTION_INVALID"),
                is_error=True,
            )

    @mcp.tool()
    async def awf_test_provider_connection(
        agent: str,
        provider: str,
        model: str,
        idempotency_key: str,
        deadline_seconds: int = 180,
        effort: str | None = None,
        profile_ref: str | None = None,
        profile: dict[str, Any] | None = None,
    ) -> Any:
        """Create a labeled isolated coding-cycle workspace; never retry it."""
        if not idempotency_key.strip() or len(idempotency_key) > 255:
            return safe_result(provider_error("IDEMPOTENCY_KEY_REQUIRED"), is_error=True)
        try:
            request = ProviderConnectionTestRequest.model_validate(
                {
                    "agent": agent,
                    "provider": provider,
                    "model": model,
                    "effort": effort,
                    "profile_ref": profile_ref,
                    "profile": profile,
                    "deadline_seconds": deadline_seconds,
                }
            )
            result = await create_connection_test(
                service, request, idempotency_key=idempotency_key.strip()
            )
            return safe_result(result.model_dump(mode="json"))
        except WorkspaceCreateIdempotencyConflictError:
            return safe_result(provider_error("IDEMPOTENCY_CONFLICT"), is_error=True)
        except WorkspaceProviderReadinessBlockedError:
            return safe_result(provider_error("PROVIDER_READINESS_PRECHECK_FAILED"), is_error=True)
        except (ValidationError, ValueError, ProfileResolutionError) as error:
            reason = str(error)
            return safe_result(
                provider_error(
                    reason
                    if reason.isupper() and len(reason) < 100
                    else "PROVIDER_SELECTION_INVALID"
                ),
                is_error=True,
            )
