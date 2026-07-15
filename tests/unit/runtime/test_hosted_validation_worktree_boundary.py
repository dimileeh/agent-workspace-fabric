"""Hosted validation must not leak Core-local worktree_path to Cloud."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from awf.profiles.models import WorkspaceProfile
from awf.runtime.hosted_delegation import HostedValidationDelegate
from tests.unit.runtime.test_hosted_validation_delegate import _config


@pytest.mark.unit
async def test_run_profile_phases_omits_core_local_worktree_path(
    tmp_path: Path,
) -> None:
    """Core-local worktree paths must never appear in hosted POST bodies."""
    seen: dict[str, Any] = {}
    core_worktree = tmp_path / "core-worktree"
    core_worktree_str = str(core_worktree)

    async def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/validation-runs":
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                202,
                json={
                    "operation_id": "val_boundary_1",
                    "workspace_id": "ws_boundary",
                    "operation_url": "/v1/operations/val_boundary_1",
                },
            )
        if request.method == "GET" and request.url.path == "/v1/operations/val_boundary_1":
            return httpx.Response(
                200,
                json={
                    "operation_id": "val_boundary_1",
                    "workspace_id": "ws_boundary",
                    "state": "succeeded",
                    "commands": [
                        {
                            "command": "uv run pytest tests/unit/foo -q",
                            "returncode": 0,
                            "duration_seconds": 0.5,
                            "stdout": "passed\n",
                            "stderr": "",
                            "phase": "validate",
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
            workspace_id="ws_boundary",
            compose_project="unused",
            compose_file=tmp_path / "missing-compose.yml",
            profile=WorkspaceProfile(name="hosted-boundary-test"),
            phase_names=("validate",),
            worktree_path=core_worktree,
            include_coverage=False,
        )

    assert "body" in seen
    body = seen["body"]
    assert core_worktree_str not in json.dumps(body, sort_keys=True)
    assert body.get("worktree_path") is None
    assert result.all_passed
    assert len(result.commands) == 1
    assert result.commands[0].command == "uv run pytest tests/unit/foo -q"
