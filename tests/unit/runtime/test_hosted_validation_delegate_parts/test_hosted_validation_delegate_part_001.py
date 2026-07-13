"""Hosted validation delegate request and probe mapping tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from awf.profiles.models import WorkspaceProfile
from awf.runtime.hosted_delegation import (
    HostedDelegationProtocolError,
    HostedValidationDelegate,
)
from awf.runtime.validation_types import ValidateCommandProbeTarget
from tests.unit.runtime.test_hosted_validation_delegate import _config


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
    assert command.stdout_path.name == "01_validate.stdout"
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
@pytest.mark.parametrize("request_kind", ["phases", "probe", "coverage"])
async def test_hosted_validation_requests_include_agent_auth_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    request_kind: str,
) -> None:
    """Hosted validation requests carry safe agent auth metadata."""
    monkeypatch.setenv("NPM_TOKEN", "npm-secret-value")
    monkeypatch.setenv("AWF_GITHUB_TOKEN", "github-secret-value")
    monkeypatch.setenv(
        "GOOGLE_APPLICATION_CREDENTIALS",
        "/core/adc/should-not-leak.json",
    )
    hosted_adc_target = "/home/agent/.config/gcloud/application_default_credentials.json"
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  agent:
    image: awf-agent-runtime:latest
    environment:
      NPM_TOKEN: ${NPM_TOKEN}
      GH_TOKEN: ${AWF_GITHUB_TOKEN}
      GITHUB_TOKEN: ${AWF_GITHUB_TOKEN}
      GOOGLE_APPLICATION_CREDENTIALS: ${GOOGLE_APPLICATION_CREDENTIALS}
    volumes:
      - /home/user/.ssh:/home/agent/.ssh:ro
      - /home/user/.codex:/home/agent/.codex:ro
      - /run/awf/hosted-auth-placeholders/home__agent__.config__gcloud__application_default_credentials.json:/home/agent/.config/gcloud/application_default_credentials.json:ro
  backend:
    image: backend:latest
""".lstrip(),
        encoding="utf-8",
    )
    seen: dict[str, Any] = {}

    async def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/validation-runs":
            body = json.loads(request.content)
            seen["body"] = body
            operation_id = f"{request_kind}_1"
            return httpx.Response(
                202,
                json={
                    "operation_id": operation_id,
                    "workspace_id": "ws_hosted",
                    "operation_url": f"/v1/operations/{operation_id}",
                },
            )
        if request.method == "GET" and request.url.path == f"/v1/operations/{request_kind}_1":
            payload: dict[str, Any] = {
                "operation_id": f"{request_kind}_1",
                "workspace_id": "ws_hosted",
                "state": "succeeded",
            }
            if request_kind == "probe":
                payload["validate_toolchain_probe"] = {
                    "missing": [],
                    "probe_errored": False,
                    "probe_ran": True,
                }
            elif request_kind == "coverage":
                payload["coverage"] = {
                    "provider": "python",
                    "percent": 99.5,
                    "minimum_percent": 99.0,
                    "enforce": True,
                    "status": "passed",
                    "reason_code": "COVERAGE_OK",
                }
            else:
                payload["commands"] = []
            return httpx.Response(200, json=payload)
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        delegate = HostedValidationDelegate(
            _config(),
            artifacts_dir=tmp_path,
            client=client,
        )
        if request_kind == "probe":
            await delegate.probe_validate_command_tools(
                workspace_id="ws_hosted",
                compose_project="awf_ws_hosted",
                compose_file=compose_file,
                profile=WorkspaceProfile(name="hosted-probe-test"),
            )
        elif request_kind == "coverage":
            await delegate.run_profile_coverage(
                workspace_id="ws_hosted",
                compose_project="awf_ws_hosted",
                compose_file=compose_file,
                profile=WorkspaceProfile(name="hosted-coverage-test"),
            )
        else:
            await delegate.run_profile_phases(
                workspace_id="ws_hosted",
                compose_project="awf_ws_hosted",
                compose_file=compose_file,
                profile=WorkspaceProfile(name="hosted-validation-test"),
                phase_names=("validate",),
                include_coverage=False,
            )

    agent_auth = seen["body"]["agent_auth"]
    assert agent_auth["schema"] == "hosted_validation_agent_auth.v1"
    assert agent_auth["env_passthrough_names"] == ["NPM_TOKEN"]
    assert agent_auth["env_passthrough_aliases"] == [
        {"target": "GH_TOKEN", "source": "AWF_GITHUB_TOKEN"},
        {"target": "GITHUB_TOKEN", "source": "AWF_GITHUB_TOKEN"},
    ]
    assert agent_auth["file_auth_mount_targets"] == [
        "/home/agent/.ssh",
        "/home/agent/.codex",
        hosted_adc_target,
    ]
    body_blob = json.dumps(seen["body"], sort_keys=True)
    assert "npm-secret-value" not in body_blob
    assert "github-secret-value" not in body_blob
    assert "/home/user/.ssh" not in body_blob
    assert "/home/user/.codex" not in body_blob
    assert "/core/adc/should-not-leak.json" not in body_blob
    assert "/run/awf/hosted-auth-placeholders" not in body_blob
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in agent_auth["env_passthrough_names"]


@pytest.mark.unit
async def test_hosted_validation_posts_rendered_stack_metadata(
    tmp_path: Path,
) -> None:
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  backend:
    build:
      context: /host/backend
      dockerfile: Dockerfile
    environment:
      PUBLIC_URL: http://backend:8000
      API_TOKEN: literal-service-secret
    depends_on:
      postgres:
        condition: service_healthy
    healthcheck:
      test:
        - CMD-SHELL
        - curl -fsS http://localhost:8000/healthz || exit 1
  agent:
    image: awf-agent-runtime:latest
    environment:
      NPM_TOKEN: literal-agent-secret
    volumes:
      - /home/user/.codex:/home/agent/.codex:ro
volumes: {}
networks:
  awf_net:
    name: awf-ws-hosted-net
""".lstrip(),
        encoding="utf-8",
    )
    seen: dict[str, Any] = {}

    async def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/validation-runs":
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
        await delegate.run_profile_phases(
            workspace_id="ws_hosted",
            compose_project="awf_ws_hosted",
            compose_file=compose_file,
            profile=WorkspaceProfile(name="hosted-test"),
            phase_names=("validate",),
            include_coverage=False,
        )

    rendered_stack = seen["body"]["rendered_stack"]
    assert rendered_stack["schema"] == "hosted_validation_rendered_stack.v1"
    assert rendered_stack["compose_project"] == "awf_ws_hosted"
    assert rendered_stack["compose_file_path"] == str(compose_file)
    assert set(rendered_stack["services"]) == {"backend"}
    backend = rendered_stack["services"]["backend"]
    assert backend["build"] == {"context": "/host/backend", "dockerfile": "Dockerfile"}
    assert backend["environment"] == {
        "PUBLIC_URL": "http://backend:8000",
        "API_TOKEN": "${API_TOKEN}",
    }
    assert backend["depends_on"] == {"postgres": {"condition": "service_healthy"}}
    assert rendered_stack["networks"] == {"awf_net": {"name": "awf-ws-hosted-net"}}
    body_blob = json.dumps(seen["body"], sort_keys=True)
    assert "literal-service-secret" not in body_blob
    assert "literal-agent-secret" not in body_blob
    assert "/home/user/.codex" not in body_blob


@pytest.mark.unit
async def test_hosted_validation_validate_toolchain_probe_posts_operation_and_maps_result(
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
                    "operation_id": "probe_1",
                    "workspace_id": "ws_hosted",
                    "operation_url": "/v1/operations/probe_1",
                },
            )
        if request.method == "GET" and request.url.path == "/v1/operations/probe_1":
            return httpx.Response(
                200,
                json={
                    "operation_id": "probe_1",
                    "workspace_id": "ws_hosted",
                    "state": "succeeded",
                    "validate_toolchain_probe": {
                        "missing": [
                            {"tool": "ruff", "command": "ruff check src/awf"},
                            {"tool": "pytest", "command": "pytest tests/unit -q"},
                        ],
                        "probe_errored": False,
                        "probe_ran": True,
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
        result = await delegate.probe_validate_command_tools(
            workspace_id="ws_hosted",
            compose_project="unused",
            compose_file=tmp_path / "missing-compose.yml",
            profile=WorkspaceProfile.model_validate(
                {
                    "name": "hosted-probe-test",
                    "phases": {
                        "validate": ["ruff check src/awf", "pytest tests/unit -q"],
                    },
                }
            ),
            pr_identity={
                "repo_url": "https://host-secret@github.com/base/repo.git",
                "pr_number": 277,
                "head_ref": "awf/ws-hosted",
            },
        )

    assert result.missing == (
        ValidateCommandProbeTarget(tool="ruff", command="ruff check src/awf"),
        ValidateCommandProbeTarget(tool="pytest", command="pytest tests/unit -q"),
    )
    assert result.probe_errored is False
    assert result.probe_ran is True
    assert seen["headers"]["authorization"] == "Bearer secret-token"
    body_blob = json.dumps(seen["body"], sort_keys=True)
    assert "secret-token" not in body_blob
    assert "host-secret" not in body_blob
    assert seen["body"]["workspace_id"] == "ws_hosted"
    assert seen["body"]["probe"] == "validate_toolchain"
    assert seen["body"]["phase_names"] == []
    assert seen["body"]["run_healthchecks"] is False
    assert seen["body"]["include_coverage"] is False
    assert seen["body"]["pr_identity"] == {
        "repo_url": "https://github.com/base/repo.git",
        "pr_number": 277,
        "head_ref": "awf/ws-hosted",
    }
    assert seen["body"]["profile"]["phases"]["validate"] == [
        {"command": "ruff check src/awf", "timeout_seconds": None, "required": True},
        {"command": "pytest tests/unit -q", "timeout_seconds": None, "required": True},
    ]


@pytest.mark.unit
@pytest.mark.parametrize("state", ["failed", "cancelled", "timed_out"])
async def test_hosted_validation_validate_toolchain_probe_terminal_failure_is_probe_error(
    tmp_path: Path,
    state: str,
) -> None:
    async def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/validation-runs":
            return httpx.Response(
                202,
                json={
                    "operation_id": "probe_1",
                    "workspace_id": "ws_hosted",
                    "operation_url": "/v1/operations/probe_1",
                },
            )
        if request.method == "GET" and request.url.path == "/v1/operations/probe_1":
            return httpx.Response(
                200,
                json={
                    "operation_id": "probe_1",
                    "workspace_id": "ws_hosted",
                    "state": state,
                    "message": "host-side probe did not complete",
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        delegate = HostedValidationDelegate(
            _config(),
            artifacts_dir=tmp_path,
            client=client,
        )
        result = await delegate.probe_validate_command_tools(
            workspace_id="ws_hosted",
            compose_project="unused",
            compose_file=tmp_path / "missing-compose.yml",
            profile=WorkspaceProfile(name="hosted-probe-test"),
        )

    assert result.missing == ()
    assert result.probe_errored is True
    assert result.probe_ran is True


@pytest.mark.unit
@pytest.mark.parametrize(
    ("probe_payload", "match"),
    [
        (None, "missing validate_toolchain_probe"),
        ({"missing": "ruff"}, "malformed missing"),
        ({"missing": ["ruff"]}, "missing item is malformed"),
    ],
)
async def test_hosted_validation_validate_toolchain_probe_rejects_malformed_payload(
    tmp_path: Path,
    probe_payload: object,
    match: str,
) -> None:
    async def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/validation-runs":
            return httpx.Response(
                202,
                json={
                    "operation_id": "probe_1",
                    "workspace_id": "ws_hosted",
                    "operation_url": "/v1/operations/probe_1",
                },
            )
        if request.method == "GET" and request.url.path == "/v1/operations/probe_1":
            payload: dict[str, object] = {
                "operation_id": "probe_1",
                "workspace_id": "ws_hosted",
                "state": "succeeded",
            }
            if probe_payload is not None:
                payload["validate_toolchain_probe"] = probe_payload
            return httpx.Response(200, json=payload)
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        delegate = HostedValidationDelegate(
            _config(),
            artifacts_dir=tmp_path,
            client=client,
        )
        with pytest.raises(HostedDelegationProtocolError, match=match):
            await delegate.probe_validate_command_tools(
                workspace_id="ws_hosted",
                compose_project="unused",
                compose_file=tmp_path / "missing-compose.yml",
                profile=WorkspaceProfile(name="hosted-probe-test"),
            )
