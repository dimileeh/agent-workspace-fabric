"""Public AWF Core discovery endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Request

from awf.service.core_discovery import (
    CoreDiscoveryResponse,
    core_discovery_payload_from_state,
)

router = APIRouter(tags=["system"])


@router.get("/.well-known/awf-core.json", response_model=CoreDiscoveryResponse)
async def core_discovery(request: Request) -> CoreDiscoveryResponse:
    return core_discovery_payload_from_state(request.app.state).to_response()
