"""Additional hosted validation delegate edge tests."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from awf.profiles.models import WorkspaceProfile
from awf.runtime.hosted_delegation import HostedDelegationConfig, HostedValidationDelegate


def _config(**overrides: object) -> HostedDelegationConfig:
    values: dict[str, object] = {
        "base_url": "https://hosted.example.test",
        "bearer_token": "secret-token",
        "poll_interval_seconds": 0.001,
        "operation_timeout_seconds": 1.0,
        "request_timeout_seconds": 1.0,
        "cancel_timeout_seconds": 1.0,
        "max_output_bytes": 100_000,
    }
    values.update(overrides)
    return HostedDelegationConfig(**values)  # type: ignore[arg-type]


@pytest.mark.unit
async def test_hosted_profile_phases_preserve_unrequested_coverage_payload_status(
    tmp_path: Path,
) -> None:
    """Unexpected coverage metadata is recorded without applying profile policy."""

    async def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/validation-runs":
            return httpx.Response(
                202,
                json={
                    "operation_id": "validate_1",
                    "workspace_id": "ws_hosted",
                    "operation_url": "/v1/operations/validate_1",
                },
            )
        if request.method == "GET" and request.url.path == "/v1/operations/validate_1":
            return httpx.Response(
                200,
                json={
                    "operation_id": "validate_1",
                    "workspace_id": "ws_hosted",
                    "state": "succeeded",
                    "commands": [],
                    "coverage": {
                        "provider": "python",
                        "percent": 82.0,
                        "minimum_percent": 99.0,
                        "enforce": False,
                        "status": "error",
                        "reason_code": "COVERAGE_PROVIDER_FAILED",
                        "gaps": [{"file": "src/awf/runtime/hosted_delegation.py"}],
                    },
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        delegate = HostedValidationDelegate(
            _config(),
            artifacts_dir=tmp_path,
            client=client,
        )
        result = await delegate.run_profile_phases(
            workspace_id="ws_hosted",
            compose_project="unused",
            compose_file=tmp_path / "missing-compose.yml",
            profile=WorkspaceProfile(name="hosted-test"),
            phase_names=("validate",),
            include_coverage=False,
        )

    assert result.commands == []
    assert result.coverage is not None
    assert result.coverage.status == "error"
    assert result.coverage.enforce is False
    assert result.coverage.reason_code == "COVERAGE_PROVIDER_FAILED"
    assert result.coverage.gaps == [{"file": "src/awf/runtime/hosted_delegation.py"}]
