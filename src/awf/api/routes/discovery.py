"""Public AWF Core discovery endpoint."""

from __future__ import annotations

from fastapi import APIRouter

from awf.service.core_discovery import (
    CoreDiscoveryResponse,
    build_core_discovery_payload,
)

router = APIRouter(tags=["system"])


@router.get("/.well-known/awf-core.json", response_model=CoreDiscoveryResponse)
async def core_discovery() -> CoreDiscoveryResponse:
    return build_core_discovery_payload().to_response()
