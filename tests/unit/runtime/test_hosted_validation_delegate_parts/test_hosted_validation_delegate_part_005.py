"""Hosted validation delegation edge tests split for line-limit guardrails."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from awf.adapters.runtime_executor import AgentRuntimeExecRequest
from awf.db.enums import AgentRuntime
from awf.profiles.models import WorkspaceProfile
from awf.runtime.hosted_delegation import (
    HostedDelegationProtocolError,
    HostedValidationDelegate,
    _hosted_validation_profile_payload,
    _hosted_validation_sanitize_environment_container,
    _hosted_validation_sanitize_secret_refs,
)
from awf.runtime.hosted_delegation_payloads import (
    _agent_start_payload,
    _hosted_validation_agent_auth_payload,
    _hosted_validation_attach_rendered_stack,
)
from tests.unit.runtime.test_hosted_validation_delegate import _config


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
async def test_hosted_validation_rejects_reordered_required_command_response(
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
                        },
                        {
                            "command": "advisory-lint",
                            "returncode": 0,
                            "duration_seconds": 0.2,
                            "stdout": "lint ok\n",
                            "stderr": "",
                            "phase": "validate",
                        },
                    ],
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-test",
            "phases": {
                "validate": [
                    {"command": "advisory-lint", "required": False},
                    {"command": "pytest -q", "required": True},
                ]
            },
        }
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        delegate = HostedValidationDelegate(
            _config(),
            artifacts_dir=tmp_path,
            client=client,
        )
        with pytest.raises(HostedDelegationProtocolError, match="command identity"):
            await delegate.run_profile_phases(
                workspace_id="ws_hosted",
                compose_project="unused",
                compose_file=tmp_path / "missing-compose.yml",
                profile=profile,
                phase_names=("validate",),
            )


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
def test_hosted_profile_env_redacts_provider_ref_values() -> None:
    """Profile env must not serialize raw provider refs for safe-named keys.

    Mirrors rendered-stack omit mode: ``PUBLIC_REF=plain-file://…`` and
    ``SERVICE_REF=env://API_TOKEN`` are credential references even when the
    env name itself is non-credential. They must not appear in hosted profile JSON.
    """
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-provider-ref-env",
            "runtime": {
                "environment": {
                    "PUBLIC_URL": "http://runtime:8000",
                    "PUBLIC_REF": "plain-file:///home/user/.awf/secrets/codex.default",
                    "SERVICE_REF": "env://API_TOKEN",
                    "KEYRING_REF": "keyring://codex/default",
                    "KEEP_REF": "${PUBLIC_HOST}",
                },
            },
            "services": [
                {
                    "name": "worker",
                    "image": "worker:latest",
                    "environment": {
                        "PUBLIC_REF": "plain-file:///home/user/.awf/secrets/github.default",
                        "SERVICE_REF": "env://OPENAI_API_KEY",
                        "REDIS_URL": "redis://cache:6379/0",
                    },
                },
            ],
        }
    )

    payload = _hosted_validation_profile_payload(profile)

    runtime_env = payload["runtime"]["environment"]
    assert runtime_env["PUBLIC_URL"] == "http://runtime:8000"
    assert runtime_env["KEEP_REF"] == "${PUBLIC_HOST}"
    assert runtime_env["PUBLIC_REF"] == "${PUBLIC_REF}"
    assert runtime_env["SERVICE_REF"] == "${SERVICE_REF}"
    assert runtime_env["KEYRING_REF"] == "${KEYRING_REF}"
    service_env = payload["services"][0]["environment"]
    assert service_env["REDIS_URL"] == "redis://cache:6379/0"
    assert service_env["PUBLIC_REF"] == "${PUBLIC_REF}"
    assert service_env["SERVICE_REF"] == "${SERVICE_REF}"
    body = json.dumps(payload, sort_keys=True)
    assert "plain-file://" not in body
    assert "env://API_TOKEN" not in body
    assert "env://OPENAI_API_KEY" not in body
    assert "keyring://codex/default" not in body
    assert "/home/user/.awf/secrets/" not in body


@pytest.mark.unit
@pytest.mark.parametrize(
    ("runtime_env", "service_env", "expected_runtime", "expected_service", "leaks"),
    [
        (
            {
                "PUBLIC_URL": "http://runtime:8000",
                "PUBLIC_CERT": ("https://svc?cert=plain-file%3A%2F%2F%2Fhome%2Fuser%2F.pgcert"),
                "SERVICE_REF": "env%3A%2F%2FPGPASSWORD",
                "KEEP_REF": "${PUBLIC_HOST}",
            },
            {
                "PUBLIC_REF": (
                    "plain-file%3A%2F%2F%2Fhome%2Fuser%2F.awf%2Fsecrets%2Fgithub.default"
                ),
                "REDIS_URL": "redis://cache:6379/0",
            },
            {
                "PUBLIC_URL": "http://runtime:8000",
                "PUBLIC_CERT": "${PUBLIC_CERT}",
                "SERVICE_REF": "${SERVICE_REF}",
                "KEEP_REF": "${PUBLIC_HOST}",
            },
            {
                "PUBLIC_REF": "${PUBLIC_REF}",
                "REDIS_URL": "redis://cache:6379/0",
            },
            (
                "plain-file%3A%2F%2F%2Fhome%2Fuser%2F.pgcert",
                "env%3A%2F%2FPGPASSWORD",
                "plain-file%3A%2F%2F%2Fhome%2Fuser%2F.awf%2Fsecrets%2Fgithub.default",
                "/home/user/.pgcert",
                "/home/user/.awf/secrets/",
            ),
        ),
    ],
)
def test_hosted_profile_env_redacts_url_encoded_provider_ref_values(
    runtime_env: dict[str, str],
    service_env: dict[str, str],
    expected_runtime: dict[str, str],
    expected_service: dict[str, str],
    leaks: tuple[str, ...],
) -> None:
    """Profile env must decode URL-component variants before accepting values.

    Encoded provider refs must not ship in hosted profile JSON even when the
    env name is non-credential and the Postgres fast-path does not apply.
    """
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-encoded-provider-ref-env",
            "runtime": {"environment": runtime_env},
            "services": [
                {
                    "name": "worker",
                    "image": "worker:latest",
                    "environment": service_env,
                },
            ],
        }
    )

    payload = _hosted_validation_profile_payload(profile)

    assert payload["runtime"]["environment"] == expected_runtime
    assert payload["services"][0]["environment"] == expected_service
    body = json.dumps(payload, sort_keys=True)
    for leak in leaks:
        assert leak not in body


@pytest.mark.unit
def test_hosted_profile_env_omits_compose_operator_arm_literal_secrets() -> None:
    """Profile env sanitize must omit Compose default/alternate arms with secrets.

    Mirrored from rendered-stack omit mode: safe target names such as
    ``CONFIG=${CONFIG:-password=…}`` must not keep credential material in the
    hosted profile block — inspect operator arms, not only credential source names.
    """
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-operator-arm-secrets",
            "runtime": {
                "environment": {
                    "PUBLIC_URL": "${PUBLIC_URL:-postgresql://user:pw@postgres/db}",
                    "NESTED_URL": (
                        "${OUTER:-${INNER:-postgresql://agent:nested-secret@postgres/db}}"
                    ),
                    "CONFIG": "${CONFIG:-password=supersecretvalue123}",
                    "TOKEN_DEFAULT": (
                        "${TOKEN_DEFAULT:-sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123}"
                    ),
                    "KEEP_REF": "${PUBLIC_HOST:-localhost}",
                },
            },
            "services": [
                {
                    "name": "worker",
                    "image": "worker:latest",
                    "environment": {
                        "CACHE_URL": "${CACHE_URL:-postgresql://cache:cache-pw@postgres/cache}",
                        "REDIS_URL": "redis://cache:6379/0",
                    },
                },
            ],
        }
    )

    payload = _hosted_validation_profile_payload(profile)

    assert payload["runtime"]["environment"] == {
        "KEEP_REF": "${PUBLIC_HOST:-localhost}",
    }
    assert payload["services"][0]["environment"] == {
        "REDIS_URL": "redis://cache:6379/0",
    }
    body = json.dumps(payload, sort_keys=True)
    assert "user:pw" not in body
    assert "nested-secret" not in body
    assert "supersecretvalue123" not in body
    assert "sk-ant-api03" not in body
    assert "cache-pw" not in body
    assert "PUBLIC_URL" not in body
    assert "NESTED_URL" not in body
    assert "CONFIG" not in body
    assert "TOKEN_DEFAULT" not in body
    assert "CACHE_URL" not in body


@pytest.mark.unit
def test_hosted_validation_profile_payload_preserves_passwordless_ssh_env_urls() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-passwordless-ssh",
            "runtime": {
                "environment": {
                    "REPOSITORY_URL": "ssh://git@github.com/org/repo.git",
                    "PRIVATE_PACKAGE_URL": "git+ssh://git@github.com/org/pkg.git",
                    "TOKENIZED_PACKAGE_URL": "git+ssh://token@github.com/org/private.git",
                },
            },
            "services": [
                {
                    "name": "builder",
                    "image": "builder:latest",
                    "environment": {
                        "DEPENDENCY_URL": "git+ssh://git@github.com/org/dep.git",
                        "FORK_URL": "ssh://git:fork-secret@github.com/org/fork.git",
                    },
                },
            ],
        }
    )

    payload = _hosted_validation_profile_payload(profile)

    assert payload["runtime"]["environment"] == {
        "REPOSITORY_URL": "ssh://git@github.com/org/repo.git",
        "PRIVATE_PACKAGE_URL": "git+ssh://git@github.com/org/pkg.git",
        "TOKENIZED_PACKAGE_URL": "${TOKENIZED_PACKAGE_URL}",
    }
    assert payload["services"][0]["environment"] == {
        "DEPENDENCY_URL": "git+ssh://git@github.com/org/dep.git",
        "FORK_URL": "${FORK_URL}",
    }


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
def test_hosted_validation_profile_payload_clears_env_provider_secrets() -> None:
    # Hosted validation Jobs reject any profile.secrets; env-provider
    # declarations with refs are cleared the same as local-file mounts.
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

    assert payload["secrets"] == []


@pytest.mark.unit
def test_hosted_validation_profile_payload_preserves_empty_services() -> None:
    payload = _hosted_validation_profile_payload(
        WorkspaceProfile.model_validate({"name": "empty-services", "services": []})
    )

    assert payload["services"] == []


@pytest.mark.unit
def test_agent_start_payload_file_auth_mount_targets_empty() -> None:
    """Repair-agent hosted start payloads match the Cloud agent-run DTO.

    AWF Cloud rejects every non-empty ``file_auth_mount_targets`` on both
    validation and agent-run request DTOs; hosted provider auth is env-secret
    ref based. Preserve passthrough names/aliases only.

    Cloud also rejects any non-null ``timeouts.idle_seconds`` because hosted
    Jobs enforce only wall deadlines. Adapter defaults still supply local
    idle=3600 and wall=7200; hosted JSON must clear idle and keep wall.
    """
    request = AgentRuntimeExecRequest(
        workspace_id="ws_hosted",
        agent_runtime=AgentRuntime.codex,
        cli_args=("codex", "exec", "-"),
        prompt_stdin=b"repair prompt",
        log_source="monitor.repair",
        model="gpt-5",
        effort="high",
        env_passthrough_names=("CODEX_API_KEY", "NPM_TOKEN"),
        env_passthrough_aliases=(("GH_TOKEN", "AWF_GITHUB_TOKEN"),),
        file_auth_mount_targets=(
            "/home/agent/.codex",
            "/home/agent/.ssh",
            "/home/agent/.gemini",
            "/home/agent/.config/gcloud/application_default_credentials.json",
        ),
        wall_timeout_seconds=7200.0,
        idle_timeout_seconds=3600.0,
    )

    payload = _agent_start_payload(request)

    assert payload["file_auth_mount_targets"] == []
    assert payload["env_passthrough_names"] == ["CODEX_API_KEY", "NPM_TOKEN"]
    assert payload["env_passthrough_aliases"] == [
        {"target": "GH_TOKEN", "source": "AWF_GITHUB_TOKEN"},
    ]
    assert payload["timeouts"] == {
        "wall_seconds": 7200.0,
        "idle_seconds": None,
    }
    body = json.dumps(payload, sort_keys=True)
    assert '"idle_seconds": null' in body
    assert '"wall_seconds": 7200.0' in body
    assert payload["cli_args"] == []
    assert "/home/agent/.codex" not in body
    assert "/home/agent/.ssh" not in body
    assert "/home/agent/.gemini" not in body
    assert "application_default_credentials.json" not in body
    assert "/home/agent" not in body


@pytest.mark.unit
def test_agent_start_payload_matches_cloud_agent_run_dto(tmp_path: Path) -> None:
    """Full agent-start JSON matches Cloud AgentRunStartRequest Phase 1 shape.

    Local adapters still supply argv, idle timeout, lifecycle phases, Postgres
    passwords, and file-auth mount paths. Hosted agent Jobs reject those; Core
    already runs setup/pre/post/cleanup and generated_setup via validation Jobs.
    """
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  postgres:
    image: postgres:16
    environment:
      POSTGRES_USER: awf
      POSTGRES_DB: awf
      POSTGRES_PASSWORD: literal-postgres-password
  backend:
    image: backend:latest
    environment:
      PUBLIC_URL: http://backend:8000
      API_TOKEN: literal-service-secret
  agent:
    image: awf-agent-runtime:latest
""".lstrip(),
        encoding="utf-8",
    )
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-agent-start-contract",
            "docker": {"mode": "none"},
            "runtime": {
                "environment": {
                    "OLLAMA_HOST": "http://ollama:11434",
                    "DATABASE_URL": (
                        "postgresql+asyncpg://awf:literal-db-secret@postgres:5432/awf"
                    ),
                }
            },
            "services": [
                {
                    "name": "postgres",
                    "image": "postgres:16",
                    "environment": {
                        "POSTGRES_USER": "awf",
                        "POSTGRES_DB": "awf",
                        "POSTGRES_PASSWORD": "literal-postgres-password",
                    },
                }
            ],
            "phases": {
                "setup": ["uv sync --extra dev"],
                "pre_agent": ["echo pre"],
                "post_agent": ["echo post"],
                "validate": ["pytest -q"],
                "cleanup": ["echo cleanup"],
            },
            "database": {
                "generated_setup": ["alembic upgrade head"],
                "pre_validation_refresh": ["echo refresh"],
            },
            "security": {"egress": {"mode": "restricted"}},
        }
    )
    request = AgentRuntimeExecRequest(
        workspace_id="ws_hosted",
        agent_runtime=AgentRuntime.codex,
        cli_args=("codex", "exec", "-"),
        prompt_stdin=b"repair prompt",
        log_source="monitor.repair",
        model="gpt-5",
        effort="high",
        env_passthrough_names=("CODEX_API_KEY",),
        env_passthrough_aliases=(("GH_TOKEN", "AWF_GITHUB_TOKEN"),),
        file_auth_mount_targets=("/home/agent/.codex", "/home/agent/.ssh"),
        profile_env=(("AWF_TASK", "repair"),),
        wall_timeout_seconds=7200.0,
        idle_timeout_seconds=3600.0,
        profile=profile,
        compose_project="awf_ws_hosted",
        compose_file=compose_file,
        repo_url="git@github.com:dimileeh/aira-web.git",
        pr_number=277,
        base_ref="development",
        head_ref="feature/ready",
    )

    payload = _agent_start_payload(request)

    assert payload["cli_args"] == []
    assert payload["timeouts"] == {"wall_seconds": 7200.0, "idle_seconds": None}
    assert payload["file_auth_mount_targets"] == []
    assert payload["model"] == "gpt-5"
    assert payload["effort"] == "high"
    assert payload["env_passthrough_names"] == ["CODEX_API_KEY"]
    assert payload["env_passthrough_aliases"] == [
        {"target": "GH_TOKEN", "source": "AWF_GITHUB_TOKEN"},
    ]
    assert payload["profile_env"] == [{"name": "AWF_TASK", "value": "repair"}]
    assert payload["pr_identity"]["pr_number"] == 277
    assert payload["profile"]["docker"]["mode"] == "compose"
    assert payload["profile"]["security"]["egress"]["mode"] == "restricted"
    assert payload["profile"]["phases"] == {
        "setup": [],
        "pre_agent": [],
        "post_agent": [],
        "validate": [
            {"command": "pytest -q", "timeout_seconds": None, "required": True},
        ],
        "cleanup": [],
    }
    assert payload["profile"]["database"]["generated_setup"] == []
    assert payload["profile"]["database"]["pre_validation_refresh"] == [
        {"command": "echo refresh", "timeout_seconds": None, "required": True},
    ]
    assert payload["profile"]["runtime"]["environment"] == {
        "OLLAMA_HOST": "http://ollama:11434",
        "DATABASE_URL": "postgresql+asyncpg://awf@postgres:5432/awf",
    }
    assert payload["profile"]["services"][0]["environment"] == {
        "POSTGRES_USER": "awf",
        "POSTGRES_DB": "awf",
        "POSTGRES_HOST_AUTH_METHOD": "trust",
    }
    postgres = payload["rendered_stack"]["services"]["postgres"]
    assert postgres["environment"] == {
        "POSTGRES_USER": "awf",
        "POSTGRES_DB": "awf",
        "POSTGRES_HOST_AUTH_METHOD": "trust",
    }
    backend = payload["rendered_stack"]["services"]["backend"]
    assert backend["environment"] == {"PUBLIC_URL": "http://backend:8000"}
    body = json.dumps(payload, sort_keys=True)
    assert '"cli_args": []' in body
    assert '"idle_seconds": null' in body
    assert "literal-postgres-password" not in body
    assert "literal-db-secret" not in body
    assert "literal-service-secret" not in body
    assert "POSTGRES_PASSWORD" not in body
    assert "API_TOKEN" not in body
    assert "codex" not in payload["cli_args"]
    assert "uv sync" not in body
    assert "alembic upgrade head" not in body
    assert "/home/agent/.codex" not in body
    assert "/home/agent/.ssh" not in body
    assert "/home/agent" not in body


@pytest.mark.unit
def test_agent_start_payload_resolves_env_file_from_worktree(tmp_path: Path) -> None:
    """Repair-agent profile must use the same worktree env_file base as validation.

    When compose lives outside the checkout, a worktree-only POSTGRES_PASSWORD
    env_file is invisible to compose_dir scanning. Without profile_base_path the
    agent-start profile omits POSTGRES_HOST_AUTH_METHOD=trust even though
    validation/probe/coverage inject it for the same workspace.
    """
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "postgres.env").write_text(
        "POSTGRES_PASSWORD=worktree-agent-env-secret\nPOSTGRES_USER=awf\n",
        encoding="utf-8",
    )
    compose_dir = tmp_path / "compose-project"
    compose_dir.mkdir()
    compose_file = compose_dir / "compose.yml"
    compose_file.write_text("services: {}\n", encoding="utf-8")

    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-agent-worktree-env-file",
            "services": [
                {
                    "name": "postgres",
                    "image": "postgres:16",
                    "env_file": "postgres.env",
                    "environment": {"POSTGRES_USER": "awf"},
                }
            ],
            "phases": {"validate": ["pytest -q"]},
        }
    )
    request = AgentRuntimeExecRequest(
        workspace_id="ws_hosted",
        agent_runtime=AgentRuntime.codex,
        cli_args=("codex", "exec", "-"),
        prompt_stdin=b"repair prompt",
        log_source="monitor.repair",
        model="gpt-5",
        effort="high",
        profile=profile,
        compose_project="awf_ws_hosted",
        compose_file=compose_file,
        worktree_path=worktree,
    )

    payload = _agent_start_payload(request)

    assert payload["profile"]["services"][0]["environment"] == {
        "POSTGRES_USER": "awf",
        "POSTGRES_HOST_AUTH_METHOD": "trust",
    }
    body = json.dumps(payload, sort_keys=True)
    assert "POSTGRES_PASSWORD" not in body
    assert "worktree-agent-env-secret" not in body


@pytest.mark.unit
def test_hosted_validation_agent_auth_file_targets_empty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Validation agent_auth keeps env auth and clears file mount targets."""
    monkeypatch.setenv("NPM_TOKEN", "npm-secret-value")
    monkeypatch.setenv("AWF_GITHUB_TOKEN", "github-secret-value")
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  agent:
    image: awf-agent-runtime:latest
    environment:
      NPM_TOKEN: ${NPM_TOKEN}
      GH_TOKEN: ${AWF_GITHUB_TOKEN}
    volumes:
      - /home/user/.ssh:/home/agent/.ssh:ro
      - /home/user/.codex:/home/agent/.codex:ro
""".lstrip(),
        encoding="utf-8",
    )

    agent_auth = _hosted_validation_agent_auth_payload(compose_file=compose_file)
    assert agent_auth is not None
    assert agent_auth["file_auth_mount_targets"] == []
    assert agent_auth["env_passthrough_names"] == ["NPM_TOKEN"]
    assert agent_auth["env_passthrough_aliases"] == [
        {"target": "GH_TOKEN", "source": "AWF_GITHUB_TOKEN"},
    ]

    envelope: dict[str, object] = {}
    _hosted_validation_attach_rendered_stack(
        envelope,
        compose_project="awf_ws_hosted",
        compose_file=compose_file,
        include_agent_auth_context=True,
    )
    attached = envelope["agent_auth"]
    assert isinstance(attached, dict)
    assert attached["file_auth_mount_targets"] == []
    body = json.dumps(envelope, sort_keys=True)
    assert "npm-secret-value" not in body
    assert "github-secret-value" not in body
    assert "/home/user/.ssh" not in body
    assert "/home/user/.codex" not in body
