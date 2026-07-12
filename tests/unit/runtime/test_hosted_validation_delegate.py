"""Hosted validation delegation tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from awf.profiles.models import WorkspaceProfile
from awf.runtime.hosted_delegation import (
    HostedDelegationConfig,
    HostedDelegationProtocolError,
    HostedValidationDelegate,
)


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


def _profile_with_runtime_secret() -> WorkspaceProfile:
    return WorkspaceProfile.model_validate(
        {
            "name": "hosted-secret-test",
            "runtime": {
                "environment": {
                    "NPM_TOKEN": "npm-profile-secret",
                    "OLLAMA_HOST": "http://ollama.profile:11434",
                }
            },
        }
    )


def _profile_with_service_secret() -> WorkspaceProfile:
    return WorkspaceProfile.model_validate(
        {
            "name": "hosted-service-secret-test",
            "services": [
                {
                    "name": "postgres",
                    "image": "postgres:16",
                    "environment": {
                        "POSTGRES_PASSWORD": "literal-service-secret",
                        "POSTGRES_USER": "awf",
                        "EXTERNAL_API_KEY": "${SERVICE_API_KEY}",
                    },
                }
            ],
        }
    )


def _profile_with_secret_ref() -> WorkspaceProfile:
    return WorkspaceProfile.model_validate(
        {
            "name": "hosted-secret-ref-test",
            "secrets": [
                {
                    "name": "codex-default",
                    "target": "/run/awf/secrets/codex-default",
                    "kind": "mount",
                    "mode": "ro",
                    "required": True,
                    "provider": "local-file",
                    "ref": "local-file:///home/user/.awf/secrets/codex.default",
                }
            ],
        }
    )


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
async def test_hosted_validation_sanitizes_literal_runtime_environment_secrets(
    tmp_path: Path,
) -> None:
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
                    "commands": [],
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
            compose_project="unused",
            compose_file=tmp_path / "missing-compose.yml",
            profile=_profile_with_runtime_secret(),
            phase_names=("validate",),
        )

    body_blob = json.dumps(seen["body"], sort_keys=True)
    assert "npm-profile-secret" not in body_blob
    assert seen["body"]["profile"]["runtime"]["environment"] == {
        "NPM_TOKEN": "${NPM_TOKEN}",
        "OLLAMA_HOST": "http://ollama.profile:11434",
    }


@pytest.mark.unit
async def test_hosted_validation_sanitizes_literal_service_environment_secrets(
    tmp_path: Path,
) -> None:
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
                    "commands": [],
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
            compose_project="unused",
            compose_file=tmp_path / "missing-compose.yml",
            profile=_profile_with_service_secret(),
            phase_names=("validate",),
        )

    body_blob = json.dumps(seen["body"], sort_keys=True)
    assert "literal-service-secret" not in body_blob
    assert seen["body"]["profile"]["services"][0]["environment"] == {
        "POSTGRES_PASSWORD": "${POSTGRES_PASSWORD}",
        "POSTGRES_USER": "awf",
        "EXTERNAL_API_KEY": "${SERVICE_API_KEY}",
    }


@pytest.mark.unit
async def test_hosted_validation_strips_profile_secret_refs(tmp_path: Path) -> None:
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
                    "commands": [],
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
            compose_project="unused",
            compose_file=tmp_path / "missing-compose.yml",
            profile=_profile_with_secret_ref(),
            phase_names=("validate",),
        )

    body_blob = json.dumps(seen["body"], sort_keys=True)
    assert "local-file:///home/user/.awf/secrets/codex.default" not in body_blob
    assert seen["body"]["profile"]["secrets"] == [
        {
            "name": "codex-default",
            "target": "/run/awf/secrets/codex-default",
            "kind": "mount",
            "mode": "ro",
            "required": True,
            "provider": "local-file",
        }
    ]


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


@pytest.mark.unit
async def test_hosted_coverage_creates_artifacts_dir_for_command_result(
    tmp_path: Path,
) -> None:
    async def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/validation-runs":
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
                        "command_result": {
                            "command": "uv run pytest --cov=awf",
                            "returncode": 0,
                            "duration_seconds": 4.2,
                            "stdout": "coverage passed\n",
                            "stderr": "",
                            "phase": "coverage",
                            "reason_code": "COMMAND_SUCCEEDED",
                        },
                    },
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    workspace_artifacts = tmp_path / "ws_hosted"
    assert not workspace_artifacts.exists()

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
        )

    assert result is not None
    assert result.command_result is not None
    assert result.command_result.stdout_path == workspace_artifacts / "999_coverage.stdout"
    assert result.command_result.stdout_path.read_text(encoding="utf-8") == "coverage passed\n"
    assert result.command_result.stderr_path == workspace_artifacts / "999_coverage.stderr"
    assert result.command_result.stderr_path.read_text(encoding="utf-8") == ""


@pytest.mark.unit
async def test_hosted_coverage_requires_payload_on_success(tmp_path: Path) -> None:
    async def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/validation-runs":
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
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        delegate = HostedValidationDelegate(
            _config(),
            artifacts_dir=tmp_path,
            client=client,
        )
        with pytest.raises(HostedDelegationProtocolError, match="missing coverage"):
            await delegate.run_profile_coverage(
                workspace_id="ws_hosted",
                compose_project="unused",
                compose_file=tmp_path / "missing-compose.yml",
                profile=WorkspaceProfile(name="hosted-test"),
            )


@pytest.mark.unit
async def test_hosted_coverage_sanitizes_literal_runtime_environment_secrets(
    tmp_path: Path,
) -> None:
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
        await delegate.run_profile_coverage(
            workspace_id="ws_hosted",
            compose_project="unused",
            compose_file=tmp_path / "missing-compose.yml",
            profile=_profile_with_runtime_secret(),
        )

    body_blob = json.dumps(seen["body"], sort_keys=True)
    assert "npm-profile-secret" not in body_blob
    assert seen["body"]["profile"]["runtime"]["environment"] == {
        "NPM_TOKEN": "${NPM_TOKEN}",
        "OLLAMA_HOST": "http://ollama.profile:11434",
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    ("state", "expected_returncode", "expected_reason_code"),
    [
        ("failed", 1, "HOSTED_VALIDATION_FAILED"),
        ("cancelled", 130, "HOSTED_VALIDATION_CANCELLED"),
        ("timed_out", 124, "HOSTED_VALIDATION_TIMED_OUT"),
    ],
)
async def test_hosted_coverage_fails_closed_when_terminal_failure_has_no_payload(
    tmp_path: Path,
    state: str,
    expected_returncode: int,
    expected_reason_code: str,
) -> None:
    async def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/validation-runs":
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
                    "state": state,
                    "message": "host-side coverage job did not produce coverage",
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
        )

    assert result is not None
    assert not result.ok
    assert result.provider == "hosted"
    assert result.status == "failed"
    assert result.reason_code == expected_reason_code
    assert result.command_result is not None
    assert result.command_result.command == "hosted coverage operation"
    assert result.command_result.phase == "coverage"
    assert result.command_result.returncode == expected_returncode
    assert result.command_result.reason_code == expected_reason_code
    assert result.command_result.stderr_path.read_text(encoding="utf-8") == (
        "host-side coverage job did not produce coverage\n"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("state", "expected_returncode", "expected_reason_code"),
    [
        ("failed", 1, "HOSTED_VALIDATION_FAILED"),
        ("cancelled", 130, "HOSTED_VALIDATION_CANCELLED"),
        ("timed_out", 124, "HOSTED_VALIDATION_TIMED_OUT"),
    ],
)
async def test_hosted_coverage_fails_closed_when_terminal_failure_has_payload(
    tmp_path: Path,
    state: str,
    expected_returncode: int,
    expected_reason_code: str,
) -> None:
    async def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/validation-runs":
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
                    "state": state,
                    "message": "host-side coverage job ended before fresh coverage",
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
        )

    assert result is not None
    assert not result.ok
    assert result.provider == "hosted"
    assert result.status == "failed"
    assert result.reason_code == expected_reason_code
    assert result.command_result is not None
    assert result.command_result.command == "hosted coverage operation"
    assert result.command_result.phase == "coverage"
    assert result.command_result.returncode == expected_returncode
    assert result.command_result.reason_code == expected_reason_code
    assert result.command_result.stderr_path.read_text(encoding="utf-8") == (
        "host-side coverage job ended before fresh coverage\n"
    )


@pytest.mark.unit
async def test_hosted_validation_sanitizes_remote_phase_before_artifact_write(
    tmp_path: Path,
) -> None:
    malicious_phase = "escape/../../owned"
    workspace_artifacts = tmp_path / "ws_hosted"
    (workspace_artifacts / "01_escape").mkdir(parents=True)

    async def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/validation-runs":
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
                            "command": "pytest -q",
                            "returncode": 0,
                            "duration_seconds": 0.2,
                            "stdout": "out\n",
                            "stderr": "err\n",
                            "phase": malicious_phase,
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
            phase_names=("validate",),
        )

    command = result.commands[0]
    assert command.phase == malicious_phase
    assert command.stdout_path.name == "01_escape_owned.stdout"
    assert command.stderr_path.name == "01_escape_owned.stderr"
    assert command.stdout_path.resolve().is_relative_to(workspace_artifacts.resolve())
    assert command.stderr_path.resolve().is_relative_to(workspace_artifacts.resolve())
    assert command.stdout_path.read_text(encoding="utf-8") == "out\n"
    assert command.stderr_path.read_text(encoding="utf-8") == "err\n"
    assert not (tmp_path / "owned.stdout").exists()
    assert not (tmp_path / "owned.stderr").exists()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("state", "expected_returncode", "expected_reason_code"),
    [
        ("failed", 1, "HOSTED_VALIDATION_FAILED"),
        ("cancelled", 130, "HOSTED_VALIDATION_CANCELLED"),
        ("timed_out", 124, "HOSTED_VALIDATION_TIMED_OUT"),
    ],
)
async def test_hosted_validation_fails_closed_when_terminal_failure_has_no_commands(
    tmp_path: Path,
    state: str,
    expected_returncode: int,
    expected_reason_code: str,
) -> None:
    async def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/validation-runs":
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
                    "state": state,
                    "message": "host-side job did not produce command results",
                    "returncode": 0,
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
        )

    assert not result.all_passed
    assert len(result.commands) == 1
    command = result.commands[0]
    assert command.command == "hosted validation operation"
    assert command.phase == "validate"
    assert command.returncode == expected_returncode
    assert command.reason_code == expected_reason_code
    assert command.stderr_path.read_text(encoding="utf-8") == (
        "host-side job did not produce command results\n"
    )


@pytest.mark.unit
async def test_hosted_validation_rejects_command_output_over_max_output_bytes(
    tmp_path: Path,
) -> None:
    async def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/validation-runs":
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
                    "state": "failed",
                    "commands": [
                        {
                            "command": "pytest -q",
                            "returncode": 1,
                            "duration_seconds": 0.2,
                            "stdout": "12345",
                            "stderr": "",
                            "phase": "validate",
                        }
                    ],
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        delegate = HostedValidationDelegate(
            _config(max_output_bytes=4),
            artifacts_dir=tmp_path,
            client=client,
        )
        with pytest.raises(HostedDelegationProtocolError, match="output exceeds"):
            await delegate.run_profile_phases(
                workspace_id="ws_hosted",
                compose_project="unused",
                compose_file=tmp_path / "missing-compose.yml",
                profile=WorkspaceProfile(name="hosted-test"),
                phase_names=("validate",),
            )

    assert not (tmp_path / "ws_hosted" / "01_validate.stdout").exists()
    assert not (tmp_path / "ws_hosted" / "01_validate.stderr").exists()
