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


@pytest.mark.unit
def test_hosted_profile_env_omits_credentials_passwordless_postgres_and_trust() -> None:
    """Cloud rejects profile ${NAME} DB/password stubs; omit, rewrite, trust."""
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-profile-env-contract",
            "runtime": {
                "environment": {
                    "OLLAMA_HOST": "http://ollama.profile:11434",
                    "NPM_TOKEN": "npm-profile-secret",
                    "DATABASE_URL": (
                        "postgresql+asyncpg://awf:literal-db-secret@postgres:5432/awf"
                    ),
                    "APP_DSN": "${DATABASE_URL}",
                    "PUBLIC_HEADER": "Bearer ${POSTGRES_PASSWORD}",
                }
            },
            "services": [
                {
                    "name": "postgres",
                    "image": "postgres:16",
                    "environment": {
                        "POSTGRES_USER": "awf",
                        "POSTGRES_DB": "awf",
                        "POSTGRES_PASSWORD": "literal-service-password",
                        "EXTERNAL_API_KEY": "${SERVICE_API_KEY}",
                    },
                },
                {
                    "name": "redis",
                    "image": "redis:7",
                    "environment": {
                        "REDIS_URL": "redis://cache:6379/0",
                        "CACHE_TOKEN": "literal-cache-token",
                    },
                },
            ],
        }
    )

    payload = _hosted_validation_profile_payload(profile)

    assert payload["runtime"]["environment"] == {
        "OLLAMA_HOST": "http://ollama.profile:11434",
        "DATABASE_URL": "postgresql+asyncpg://awf@postgres:5432/awf",
    }
    assert payload["services"][0]["environment"] == {
        "POSTGRES_USER": "awf",
        "POSTGRES_DB": "awf",
        "POSTGRES_HOST_AUTH_METHOD": "trust",
    }
    assert payload["services"][1]["environment"] == {
        "REDIS_URL": "redis://cache:6379/0",
    }
    body = json.dumps(payload, sort_keys=True)
    assert "literal-db-secret" not in body
    assert "literal-service-password" not in body
    assert "literal-cache-token" not in body
    assert "${DATABASE_URL}" not in body
    assert "${POSTGRES_PASSWORD}" not in body
    assert "POSTGRES_PASSWORD" not in body
    assert "NPM_TOKEN" not in body
    assert "EXTERNAL_API_KEY" not in body
    assert "CACHE_TOKEN" not in body


@pytest.mark.unit
def test_hosted_profile_docker_mode_none_becomes_compose_with_sidecars(
    tmp_path: Path,
) -> None:
    """Non-empty rendered stack + profile services: hosted none → compose."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  postgres:
    image: postgres:16
    environment:
      POSTGRES_USER: awf
""".lstrip(),
        encoding="utf-8",
    )
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-mode-none-sidecars",
            "docker": {"mode": "none"},
            "services": [{"name": "postgres", "image": "postgres:16"}],
        }
    )
    payload: dict[str, Any] = {
        "profile": _hosted_validation_profile_payload(profile),
    }

    _hosted_validation_attach_rendered_stack(
        payload,
        compose_project="awf_ws_hosted",
        compose_file=compose_file,
        omit_credential_env_keys=True,
    )

    assert payload["profile"]["docker"]["mode"] == "compose"
    assert "postgres" in payload["rendered_stack"]["services"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("docker_mode", "profile_services", "compose_body", "expected_mode"),
    [
        (
            "dind",
            [{"name": "postgres", "image": "postgres:16"}],
            "services:\n  postgres:\n    image: postgres:16\n",
            "dind",
        ),
        (
            "none",
            [],
            "services:\n  postgres:\n    image: postgres:16\n",
            "none",
        ),
        (
            "none",
            [{"name": "postgres", "image": "postgres:16"}],
            "services:\n  agent:\n    image: awf-agent:latest\n",
            "none",
        ),
    ],
)
def test_hosted_profile_docker_mode_none_not_converted_without_sidecars(
    tmp_path: Path,
    docker_mode: str,
    profile_services: list[dict[str, str]],
    compose_body: str,
    expected_mode: str,
) -> None:
    """Never convert dind, empty stacks, or profiles without services."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(compose_body, encoding="utf-8")
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-mode-guard",
            "docker": {"mode": docker_mode},
            "services": profile_services,
        }
    )
    payload: dict[str, Any] = {
        "profile": _hosted_validation_profile_payload(profile),
    }

    _hosted_validation_attach_rendered_stack(
        payload,
        compose_project="awf_ws_hosted",
        compose_file=compose_file,
        omit_credential_env_keys=True,
    )

    assert payload["profile"]["docker"]["mode"] == expected_mode


@pytest.mark.unit
def test_hosted_profile_passwordless_postgres_url_edges() -> None:
    """Only Postgres URLs with a password arm are rewritten."""
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-profile-pg-url-edges",
            "runtime": {
                "environment": {
                    "ALREADY_OPEN": "postgresql://awf@postgres:5432/awf",
                    "STRIPPED_USERLESS": ("postgresql://:literal-only-secret@postgres:5432/awf"),
                    "HTTPS_URL": "https://user:not-postgres@example.test/db",
                    "SAFE_HOST": "http://ollama:11434",
                }
            },
        }
    )

    payload = _hosted_validation_profile_payload(profile)

    assert payload["runtime"]["environment"] == {
        "ALREADY_OPEN": "postgresql://awf@postgres:5432/awf",
        "STRIPPED_USERLESS": "postgresql://postgres:5432/awf",
        "HTTPS_URL": "${HTTPS_URL}",
        "SAFE_HOST": "http://ollama:11434",
    }
    body = json.dumps(payload, sort_keys=True)
    assert "literal-only-secret" not in body
    assert "not-postgres" not in body


@pytest.mark.unit
@pytest.mark.parametrize(
    ("database_url", "secret"),
    [
        (
            "postgresql://awf:userinfo-pw@postgres:5432/awf?sslpassword=query-pg-secret",
            "query-pg-secret",
        ),
        (
            "postgresql+asyncpg://awf@postgres:5432/awf#password=fragment-pg-secret",
            "fragment-pg-secret",
        ),
        (
            "postgres://postgres:5432/awf?sslpassword=host-only-query-secret",
            "host-only-query-secret",
        ),
    ],
)
def test_hosted_profile_passwordless_postgres_omits_query_fragment_credentials(
    database_url: str,
    secret: str,
) -> None:
    """Passwordless Postgres rewrite must not preserve query/fragment secrets."""
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-profile-pg-url-query-creds",
            "runtime": {
                "environment": {
                    "DATABASE_URL": database_url,
                    "KEEP_OPEN": "postgresql://awf@postgres:5432/awf?sslmode=require",
                    "OLLAMA_HOST": "http://ollama.profile:11434",
                }
            },
        }
    )

    payload = _hosted_validation_profile_payload(profile)

    assert payload["runtime"]["environment"] == {
        "KEEP_OPEN": "postgresql://awf@postgres:5432/awf?sslmode=require",
        "OLLAMA_HOST": "http://ollama.profile:11434",
    }
    body = json.dumps(payload, sort_keys=True)
    assert secret not in body
    assert "userinfo-pw" not in body
    assert "DATABASE_URL" not in body
    assert "${DATABASE_URL}" not in body


@pytest.mark.unit
@pytest.mark.parametrize(
    ("database_url", "rewritten", "leak"),
    [
        (
            "postgresql://${POSTGRES_PASSWORD}@postgres:5432/awf",
            "postgresql://postgres:5432/awf",
            "${POSTGRES_PASSWORD}",
        ),
        (
            "postgresql+asyncpg://%24%7BPOSTGRES_PASSWORD%7D@postgres:5432/awf",
            "postgresql+asyncpg://postgres:5432/awf",
            "POSTGRES_PASSWORD",
        ),
        (
            "postgresql://ghp_exampleusernameonlytoken12@postgres:5432/awf",
            "postgresql://postgres:5432/awf",
            "ghp_exampleusernameonlytoken12",
        ),
        (
            "postgres://${DATABASE_URL}@postgres:5432/awf",
            "postgres://postgres:5432/awf",
            "${DATABASE_URL}",
        ),
        (
            "postgresql://env%3A%2F%2Fpg_secret_ref@postgres:5432/awf",
            "postgresql://postgres:5432/awf",
            "env://pg_secret_ref",
        ),
        # Password arm present: strip password AND drop credential usernames.
        (
            "postgresql://${POSTGRES_PASSWORD}:x@postgres:5432/awf",
            "postgresql://postgres:5432/awf",
            "${POSTGRES_PASSWORD}",
        ),
        (
            "postgresql://ghp_examplepasswordarmtoken12:x@postgres:5432/awf",
            "postgresql://postgres:5432/awf",
            "ghp_examplepasswordarmtoken12",
        ),
        (
            "postgresql+asyncpg://%24%7BPOSTGRES_PASSWORD%7D:lit-pw@postgres:5432/awf",
            "postgresql+asyncpg://postgres:5432/awf",
            "POSTGRES_PASSWORD",
        ),
    ],
)
def test_hosted_profile_passwordless_postgres_strips_username_only_credentials(
    database_url: str,
    rewritten: str,
    leak: str,
) -> None:
    """Credential usernames must be stripped even when a password arm is rewritten."""
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-profile-pg-url-userinfo-creds",
            "runtime": {
                "environment": {
                    "DATABASE_URL": database_url,
                    "KEEP_OPEN": "postgresql://awf@postgres:5432/awf",
                    "OLLAMA_HOST": "http://ollama.profile:11434",
                }
            },
        }
    )

    payload = _hosted_validation_profile_payload(profile)

    assert payload["runtime"]["environment"] == {
        "DATABASE_URL": rewritten,
        "KEEP_OPEN": "postgresql://awf@postgres:5432/awf",
        "OLLAMA_HOST": "http://ollama.profile:11434",
    }
    body = json.dumps(payload, sort_keys=True)
    assert leak not in body


@pytest.mark.unit
@pytest.mark.parametrize(
    ("database_url", "leak"),
    [
        (
            "postgresql://postgres@postgres/db/${API_TOKEN}",
            "${API_TOKEN}",
        ),
        (
            "postgresql://awf@postgres:5432/awf?options=${API_TOKEN}",
            "${API_TOKEN}",
        ),
        (
            "postgresql+asyncpg://awf@postgres:5432/awf#note=${DATABASE_URL}",
            "${DATABASE_URL}",
        ),
        (
            "postgres://postgres:5432/awf/${POSTGRES_PASSWORD}",
            "${POSTGRES_PASSWORD}",
        ),
    ],
)
def test_hosted_profile_passwordless_postgres_omits_credential_source_interpolations(
    database_url: str,
    leak: str,
) -> None:
    """Passwordless Postgres keep-path must still omit credential-source refs."""
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-profile-pg-url-cred-interp",
            "runtime": {
                "environment": {
                    "DATABASE_URL": database_url,
                    "KEEP_OPEN": "postgresql://awf@postgres:5432/awf?sslmode=require",
                    "OLLAMA_HOST": "http://ollama.profile:11434",
                }
            },
        }
    )

    payload = _hosted_validation_profile_payload(profile)

    assert payload["runtime"]["environment"] == {
        "KEEP_OPEN": "postgresql://awf@postgres:5432/awf?sslmode=require",
        "OLLAMA_HOST": "http://ollama.profile:11434",
    }
    body = json.dumps(payload, sort_keys=True)
    assert leak not in body
    assert "DATABASE_URL" not in body
    assert "${DATABASE_URL}" not in body
