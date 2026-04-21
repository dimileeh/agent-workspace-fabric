"""Liveness/health endpoint.

Kept intentionally dependency-free: no DB query, no Docker daemon check, no secrets
read. Its job is to report that the HTTP stack itself is up so external probes can
distinguish "AWF process is alive" from "AWF depends on X which is down."

Separate readiness/dependency checks will live under /readyz (Phase 1.5+) and will
verify DB + Docker reachability.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from awf import __version__

router = APIRouter(tags=["system"])


class HealthResponse(BaseModel):
    """Shape of the /healthz response.

    Declared as a Pydantic model (rather than a bare dict) so the OpenAPI spec
    documents the contract and downstream MCP/REST clients get typed bindings.
    """

    status: str
    service: str
    version: str


@router.get("/healthz", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    return HealthResponse(status="ok", service="awf", version=__version__)
