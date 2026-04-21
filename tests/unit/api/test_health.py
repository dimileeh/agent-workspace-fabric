"""Health endpoint — the first contract AWF guarantees to operators and uptime probes.

The contract under test:
    GET /healthz returns 200 with a JSON body shaped as:
        {"status": "ok", "service": "awf", "version": "<semver>"}

Rationale:
    - Operators (and k8s/Cloud Run) need a liveness probe that never depends on
      external services (DB, Docker daemon) so a single dependency outage doesn't
      flap the whole control plane.
    - The version field is non-optional so deploys can verify the rolled-out build
      matches expectations.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from awf import __version__


@pytest.mark.unit
async def test_healthz_returns_200(client: AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200


@pytest.mark.unit
async def test_healthz_returns_expected_json_shape(client: AsyncClient) -> None:
    response = await client.get("/healthz")
    body = response.json()

    assert body == {"status": "ok", "service": "awf", "version": __version__}


@pytest.mark.unit
async def test_healthz_does_not_require_auth(client: AsyncClient) -> None:
    """Liveness probes must be reachable without credentials.

    Uptime monitors and cluster health checks don't authenticate; a 401/403 on this
    endpoint would cause false outage alerts.
    """
    response = await client.get("/healthz")
    assert response.status_code != 401
    assert response.status_code != 403
