"""Hosted validation delegation edge tests split for line-limit guardrails."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from awf.profiles.models import WorkspaceProfile
from awf.runtime.hosted_delegation import (
    HostedDelegationConfig,
    HostedDelegationProtocolError,
    HostedValidationDelegate,
    _hosted_validation_profile_payload,
    _hosted_validation_sanitize_environment_container,
    _hosted_validation_sanitize_secret_refs,
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


@pytest.mark.unit
async def test_hosted_validation_rejects_non_string_command_stdout(tmp_path: Path) -> None:
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
                            "stdout": 7,
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
        with pytest.raises(HostedDelegationProtocolError, match="missing stdout"):
            await delegate.run_profile_phases(
                workspace_id="ws_hosted",
                compose_project="unused",
                compose_file=tmp_path / "missing-compose.yml",
                profile=WorkspaceProfile(name="hosted-test"),
                phase_names=("validate",),
            )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("poll_content", "match"),
    [
        (b"not-json", "non-json"),
        (b"[]", "non-object"),
    ],
)
async def test_hosted_validation_rejects_non_object_poll_response(
    tmp_path: Path,
    poll_content: bytes,
    match: str,
) -> None:
    cancel_paths: list[str] = []

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
            return httpx.Response(200, content=poll_content)
        if request.method == "POST" and request.url.path == "/v1/operations/val_1/cancel":
            cancel_paths.append(request.url.path)
            return httpx.Response(202, json={"state": "cancelled"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        delegate = HostedValidationDelegate(
            _config(),
            artifacts_dir=tmp_path,
            client=client,
        )
        with pytest.raises(HostedDelegationProtocolError, match=match):
            await delegate.run_profile_phases(
                workspace_id="ws_hosted",
                compose_project="unused",
                compose_file=tmp_path / "missing-compose.yml",
                profile=WorkspaceProfile(name="hosted-test"),
                phase_names=("validate",),
            )

    assert cancel_paths == ["/v1/operations/val_1/cancel"]


@pytest.mark.unit
async def test_hosted_validation_accepts_empty_successful_commands_for_no_op_profile(
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
        result = await delegate.run_profile_phases(
            workspace_id="ws_hosted",
            compose_project="unused",
            compose_file=tmp_path / "missing-compose.yml",
            profile=WorkspaceProfile(name="hosted-test"),
            phase_names=("validate",),
        )

    assert result.all_passed
    assert result.commands == []


@pytest.mark.unit
async def test_hosted_validation_rejects_empty_successful_commands_when_commands_expected(
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
                    "state": "succeeded",
                    "commands": [],
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-test",
            "phases": {"validate": ["pytest -q"]},
        }
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        delegate = HostedValidationDelegate(
            _config(),
            artifacts_dir=tmp_path,
            client=client,
        )
        with pytest.raises(HostedDelegationProtocolError, match="missing command evidence"):
            await delegate.run_profile_phases(
                workspace_id="ws_hosted",
                compose_project="unused",
                compose_file=tmp_path / "missing-compose.yml",
                profile=profile,
                phase_names=("validate",),
            )


@pytest.mark.unit
async def test_hosted_validation_rejects_partial_successful_commands_when_commands_expected(
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
                    "state": "succeeded",
                    "commands": [
                        {
                            "command": "pytest -q",
                            "returncode": 0,
                            "duration_seconds": 0.2,
                            "stdout": "ok\n",
                            "stderr": "",
                            "phase": "validate",
                        }
                    ],
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-test",
            "phases": {"validate": ["pytest -q", "ruff check src/awf"]},
        }
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        delegate = HostedValidationDelegate(
            _config(),
            artifacts_dir=tmp_path,
            client=client,
        )
        with pytest.raises(HostedDelegationProtocolError, match="missing command evidence"):
            await delegate.run_profile_phases(
                workspace_id="ws_hosted",
                compose_project="unused",
                compose_file=tmp_path / "missing-compose.yml",
                profile=profile,
                phase_names=("validate",),
            )


@pytest.mark.unit
async def test_hosted_validation_uses_profile_required_flag_for_required_command(
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
                    "state": "succeeded",
                    "commands": [
                        {
                            "command": "pytest -q",
                            "returncode": 1,
                            "duration_seconds": 0.2,
                            "stdout": "",
                            "stderr": "failed\n",
                            "phase": "validate",
                            "required": False,
                        }
                    ],
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-test",
            "phases": {"validate": [{"command": "pytest -q", "required": True}]},
        }
    )
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
            profile=profile,
            phase_names=("validate",),
        )

    assert result.all_passed is False
    assert result.commands[0].required is True
    assert result.first_failure is result.commands[0]


@pytest.mark.unit
async def test_hosted_validation_uses_profile_required_flag_for_advisory_command(
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
                    "state": "succeeded",
                    "commands": [
                        {
                            "command": "advisory-lint",
                            "returncode": 1,
                            "duration_seconds": 0.2,
                            "stdout": "",
                            "stderr": "lint warning\n",
                            "phase": "validate",
                            "required": True,
                        }
                    ],
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-test",
            "phases": {"validate": [{"command": "advisory-lint", "required": False}]},
        }
    )
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
            profile=profile,
            phase_names=("validate",),
        )

    assert result.all_passed is True
    assert result.commands[0].required is False
    assert result.first_failure is None


@pytest.mark.unit
async def test_hosted_validation_timeout_posts_cancel_and_raises_protocol_error(
    tmp_path: Path,
) -> None:
    cancel_paths: list[str] = []

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
                json={"operation_id": "val_1", "workspace_id": "ws_hosted", "state": "running"},
            )
        if request.method == "POST" and request.url.path == "/v1/operations/val_1/cancel":
            cancel_paths.append(request.url.path)
            return httpx.Response(202, json={"state": "cancelled"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        delegate = HostedValidationDelegate(
            _config(operation_timeout_seconds=0.003),
            artifacts_dir=tmp_path,
            client=client,
        )
        with pytest.raises(HostedDelegationProtocolError, match="operation timed out"):
            await delegate.run_profile_phases(
                workspace_id="ws_hosted",
                compose_project="unused",
                compose_file=tmp_path / "missing-compose.yml",
                profile=WorkspaceProfile(name="hosted-test"),
                phase_names=("validate",),
            )

    assert cancel_paths == ["/v1/operations/val_1/cancel"]


@pytest.mark.unit
async def test_hosted_validation_cancellation_posts_cancel(tmp_path: Path) -> None:
    started_poll = asyncio.Event()
    cancel_paths: list[str] = []

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
            started_poll.set()
            await asyncio.sleep(10)
        if request.method == "POST" and request.url.path == "/v1/operations/val_1/cancel":
            cancel_paths.append(request.url.path)
            return httpx.Response(202, json={"state": "cancelled"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        delegate = HostedValidationDelegate(
            _config(),
            artifacts_dir=tmp_path,
            client=client,
        )
        task = asyncio.create_task(
            delegate.run_profile_phases(
                workspace_id="ws_hosted",
                compose_project="unused",
                compose_file=tmp_path / "missing-compose.yml",
                profile=WorkspaceProfile(name="hosted-test"),
                phase_names=("validate",),
            )
        )
        await started_poll.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert cancel_paths == ["/v1/operations/val_1/cancel"]


@pytest.mark.unit
async def test_hosted_validation_start_cancellation_raises_without_cancel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delegate = HostedValidationDelegate(_config(), artifacts_dir=tmp_path)
    cancel_called = False

    async def _start_operation(
        _client: httpx.AsyncClient,
        *,
        workspace_id: str,
        start_path: str,
        payload: dict[str, Any],
    ) -> Any:
        assert workspace_id == "ws_hosted"
        assert start_path == "/v1/validation-runs"
        assert payload["workspace_id"] == "ws_hosted"
        raise asyncio.CancelledError

    async def _cancel_operation(_client: httpx.AsyncClient, _operation: object) -> None:
        nonlocal cancel_called
        cancel_called = True

    monkeypatch.setattr(delegate, "_start_operation", _start_operation)
    monkeypatch.setattr(
        "awf.runtime.hosted_delegation._cancel_operation",
        _cancel_operation,
    )

    with pytest.raises(asyncio.CancelledError):
        await delegate.run_profile_phases(
            workspace_id="ws_hosted",
            compose_project="unused",
            compose_file=tmp_path / "missing-compose.yml",
            profile=WorkspaceProfile(name="hosted-test"),
            phase_names=("validate",),
        )

    assert cancel_called is False


@pytest.mark.unit
async def test_hosted_validation_start_timeout_raises_protocol_error_without_cancel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delegate = HostedValidationDelegate(_config(), artifacts_dir=tmp_path)
    cancel_called = False

    async def _start_operation(
        _client: httpx.AsyncClient,
        *,
        workspace_id: str,
        start_path: str,
        payload: dict[str, Any],
    ) -> Any:
        assert workspace_id == "ws_hosted"
        assert start_path == "/v1/validation-runs"
        assert payload["workspace_id"] == "ws_hosted"
        raise TimeoutError

    async def _cancel_operation(_client: httpx.AsyncClient, _operation: object) -> None:
        nonlocal cancel_called
        cancel_called = True

    monkeypatch.setattr(delegate, "_start_operation", _start_operation)
    monkeypatch.setattr(
        "awf.runtime.hosted_delegation._cancel_operation",
        _cancel_operation,
    )

    with pytest.raises(HostedDelegationProtocolError, match="operation timed out"):
        await delegate.run_profile_phases(
            workspace_id="ws_hosted",
            compose_project="unused",
            compose_file=tmp_path / "missing-compose.yml",
            profile=WorkspaceProfile(name="hosted-test"),
            phase_names=("validate",),
        )

    assert cancel_called is False


@pytest.mark.unit
def test_hosted_validation_sanitizers_ignore_malformed_optional_containers() -> None:
    non_list_secrets: dict[str, object] = {"ref": "local-file:///should-not-change"}
    mixed_secrets: list[object] = [
        "not-a-secret-object",
        {"name": "codex", "ref": "local-file:///home/user/.awf/codex"},
    ]
    runtime_container: dict[str, object] = {"environment": "not-a-mapping"}
    service_container: object = "not-a-mapping"

    _hosted_validation_sanitize_secret_refs(non_list_secrets)
    _hosted_validation_sanitize_secret_refs(mixed_secrets)
    _hosted_validation_sanitize_environment_container(runtime_container)
    _hosted_validation_sanitize_environment_container(service_container)

    assert non_list_secrets == {"ref": "local-file:///should-not-change"}
    assert mixed_secrets == ["not-a-secret-object", {"name": "codex"}]
    assert runtime_container == {"environment": "not-a-mapping"}
    assert service_container == "not-a-mapping"


@pytest.mark.unit
def test_hosted_validation_sanitizer_preserves_only_env_source_name_refs() -> None:
    secrets: list[object] = [
        {"name": "prefixed", "kind": "env", "provider": "env", "ref": "env/AWF_NPM_TOKEN"},
        {"name": "bare", "kind": "env", "provider": "env", "ref": "AWF_PIP_TOKEN"},
        {"name": "path", "kind": "env", "provider": "env", "ref": "/home/user/.npm-token"},
        {"name": "nested", "kind": "env", "provider": "env", "ref": "env/team/NPM_TOKEN"},
        {"name": "nonstr", "kind": "env", "provider": "env", "ref": 7},
        {"name": "github", "kind": "env", "provider": "github", "ref": "token"},
        {"name": "mount", "kind": "mount", "provider": "env", "ref": "env/AWF_MOUNT_TOKEN"},
    ]

    _hosted_validation_sanitize_secret_refs(secrets)

    assert secrets == [
        {"name": "prefixed", "kind": "env", "provider": "env", "ref": "env/AWF_NPM_TOKEN"},
        {"name": "bare", "kind": "env", "provider": "env", "ref": "AWF_PIP_TOKEN"},
        {"name": "path", "kind": "env", "provider": "env"},
        {"name": "nested", "kind": "env", "provider": "env"},
        {"name": "nonstr", "kind": "env", "provider": "env"},
        {"name": "github", "kind": "env", "provider": "github"},
        {"name": "mount", "kind": "mount", "provider": "env"},
    ]


@pytest.mark.unit
def test_hosted_validation_profile_payload_preserves_env_secret_source_alias() -> None:
    payload = _hosted_validation_profile_payload(
        WorkspaceProfile.model_validate(
            {
                "name": "env-secret-alias",
                "secrets": [
                    {
                        "name": "npm-token",
                        "target": "NPM_TOKEN",
                        "kind": "env",
                        "provider": "env",
                        "ref": "env/AWF_NPM_TOKEN",
                    }
                ],
            }
        )
    )

    assert payload["secrets"] == [
        {
            "name": "npm-token",
            "target": "NPM_TOKEN",
            "kind": "env",
            "mode": "ro",
            "required": True,
            "provider": "env",
            "ref": "env/AWF_NPM_TOKEN",
        }
    ]


@pytest.mark.unit
def test_hosted_validation_profile_payload_preserves_empty_services() -> None:
    payload = _hosted_validation_profile_payload(
        WorkspaceProfile.model_validate({"name": "empty-services", "services": []})
    )

    assert payload["services"] == []
