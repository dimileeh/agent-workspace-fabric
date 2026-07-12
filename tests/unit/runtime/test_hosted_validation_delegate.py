"""Hosted validation delegation tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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
async def test_hosted_validation_posts_operation_and_maps_validation_result(
    tmp_path: Path,
) -> None:
    seen: dict[str, Any] = {}

    async def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/validation-runs":
            seen["headers"] = dict(request.headers)
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                202,
                json={
                    "operation_id": "val_1",
                    "workspace_id": "ws_hosted",
                    "operation_url": "/v1/operations/val_1",
                },
            )
        if request.method == "GET" and request.url.path == "/v1/operations/val_1":
            return httpx.Response(
                200,
                json={
                    "operation_id": "val_1",
                    "workspace_id": "ws_hosted",
                    "state": "succeeded",
                    "commands": [
                        {
                            "command": "uv run pytest tests/unit/foo -q",
                            "returncode": 0,
                            "duration_seconds": 1.25,
                            "stdout": "passed\n",
                            "stderr": "",
                            "phase": "validate",
                            "reason_code": "COMMAND_FAILED",
                            "stream_ids": {
                                "stdout": "hosted.validation.stdout",
                                "stderr": "hosted.validation.stderr",
                            },
                        }
                    ],
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
            phase_names=("post_agent", "validate"),
            run_healthchecks=True,
            worktree_path=tmp_path / "worktree",
            include_coverage=False,
            pr_identity={
                "repo_url": "git@github.com:dimileeh/aira-web.git",
                "pr_number": 277,
                "head_ref": "feature/ready",
            },
        )

    assert result.all_passed
    assert len(result.commands) == 1
    command = result.commands[0]
    assert command.command == "uv run pytest tests/unit/foo -q"
    assert command.stdout_path.read_text(encoding="utf-8") == "passed\n"
    assert command.stderr_path.read_text(encoding="utf-8") == ""
    assert command.stream_ids == {
        "stdout": "hosted.validation.stdout",
        "stderr": "hosted.validation.stderr",
    }
    assert seen["headers"]["authorization"] == "Bearer secret-token"
    body_blob = json.dumps(seen["body"], sort_keys=True)
    assert "secret-token" not in body_blob
    assert seen["body"]["workspace_id"] == "ws_hosted"
    assert seen["body"]["phase_names"] == ["post_agent", "validate"]
    assert seen["body"]["run_healthchecks"] is True
    assert seen["body"]["include_coverage"] is False
    assert seen["body"]["pr_identity"]["pr_number"] == 277


@pytest.mark.unit
async def test_hosted_coverage_posts_pr_identity(tmp_path: Path) -> None:
    """Coverage-only hosted validation must preserve adopted PR identity."""
    seen: dict[str, Any] = {}

    async def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/validation-runs":
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                202,
                json={
                    "operation_id": "coverage_1",
                    "workspace_id": "ws_hosted",
                    "operation_url": "/v1/operations/coverage_1",
                },
            )
        if request.method == "GET" and request.url.path == "/v1/operations/coverage_1":
            return httpx.Response(
                200,
                json={
                    "operation_id": "coverage_1",
                    "workspace_id": "ws_hosted",
                    "state": "succeeded",
                    "coverage": {
                        "provider": "python",
                        "percent": 99.5,
                        "minimum_percent": 99.0,
                        "enforce": True,
                        "status": "passed",
                        "reason_code": "COVERAGE_OK",
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
        result = await delegate.run_profile_coverage(
            workspace_id="ws_hosted",
            compose_project="unused",
            compose_file=tmp_path / "missing-compose.yml",
            profile=WorkspaceProfile(name="hosted-test"),
            pr_identity={
                "repo_url": "git@github.com:dimileeh/aira-web.git",
                "pr_number": 277,
                "head_ref": "feature/ready",
            },
        )

    assert result is not None
    assert result.status == "passed"
    assert seen["body"]["workspace_id"] == "ws_hosted"
    assert seen["body"]["phase_names"] == ["coverage"]
    assert seen["body"]["include_coverage"] is True
    assert seen["body"]["pr_identity"]["pr_number"] == 277
