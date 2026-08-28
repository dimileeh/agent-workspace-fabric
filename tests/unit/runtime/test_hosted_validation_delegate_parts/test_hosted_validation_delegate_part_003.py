"""Hosted validation delegate coverage tests."""

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
from tests.unit.runtime.test_hosted_validation_delegate import (
    _config,
    _profile_with_runtime_secret,
)


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
async def test_hosted_coverage_rejects_passed_command_gate_without_command_result(
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
                    },
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-test",
            "validation": {
                "coverage": {
                    "minimum_percent": 99.0,
                    "command": "uv run pytest --cov=awf",
                },
                "strategy": {"final_gate": "coverage"},
            },
        }
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        delegate = HostedValidationDelegate(
            _config(),
            artifacts_dir=tmp_path,
            client=client,
        )
        with pytest.raises(HostedDelegationProtocolError, match="missing command evidence"):
            await delegate.run_profile_coverage(
                workspace_id="ws_hosted",
                compose_project="unused",
                compose_file=tmp_path / "missing-compose.yml",
                profile=profile,
            )


@pytest.mark.unit
async def test_hosted_combined_validation_rejects_passed_coverage_without_command_result(
    tmp_path: Path,
) -> None:
    """Combined hosted validation must require coverage command evidence when configured."""
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-combined-coverage-evidence",
            "validation": {
                "coverage": {
                    "minimum_percent": 99.0,
                    "command": "uv run pytest --cov=awf",
                },
                "strategy": {"final_gate": "coverage"},
            },
        }
    )

    async def _handler(request: httpx.Request) -> httpx.Response:
        """Return combined validation success without coverage command evidence."""

        if request.method == "POST" and request.url.path == "/v1/validation-runs":
            return httpx.Response(
                202,
                json={
                    "operation_id": "val_combined_cov",
                    "workspace_id": "ws_hosted",
                    "operation_url": "/v1/operations/val_combined_cov",
                },
            )
        if request.method == "GET" and request.url.path == "/v1/operations/val_combined_cov":
            return httpx.Response(
                200,
                json={
                    "operation_id": "val_combined_cov",
                    "workspace_id": "ws_hosted",
                    "state": "succeeded",
                    "commands": [
                        {
                            "command": "pytest -q",
                            "returncode": 0,
                            "duration_seconds": 0.2,
                            "stdout": "",
                            "stderr": "",
                            "phase": "validate",
                        }
                    ],
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
        with pytest.raises(HostedDelegationProtocolError, match="missing command evidence"):
            await delegate.run_profile_phases(
                workspace_id="ws_hosted",
                compose_project="unused",
                compose_file=tmp_path / "missing-compose.yml",
                profile=profile,
                phase_names=("validate",),
                include_coverage=True,
            )


@pytest.mark.unit
async def test_hosted_combined_validation_rejects_success_omitting_coverage(
    tmp_path: Path,
) -> None:
    """Combined hosted success must fail closed when requested coverage is absent."""
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-combined-missing-coverage",
            "validation": {
                "coverage": {
                    "minimum_percent": 99.0,
                    "command": "uv run pytest --cov=awf",
                },
                "strategy": {"final_gate": "coverage"},
            },
        }
    )

    async def _handler(request: httpx.Request) -> httpx.Response:
        """Return combined success with validate evidence but no coverage field."""

        if request.method == "POST" and request.url.path == "/v1/validation-runs":
            return httpx.Response(
                202,
                json={
                    "operation_id": "val_combined_omit_cov",
                    "workspace_id": "ws_hosted",
                    "operation_url": "/v1/operations/val_combined_omit_cov",
                },
            )
        if request.method == "GET" and request.url.path == "/v1/operations/val_combined_omit_cov":
            return httpx.Response(
                200,
                json={
                    "operation_id": "val_combined_omit_cov",
                    "workspace_id": "ws_hosted",
                    "state": "succeeded",
                    "commands": [
                        {
                            "command": "pytest -q",
                            "returncode": 0,
                            "duration_seconds": 0.2,
                            "stdout": "",
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
        with pytest.raises(HostedDelegationProtocolError, match="missing coverage"):
            await delegate.run_profile_phases(
                workspace_id="ws_hosted",
                compose_project="unused",
                compose_file=tmp_path / "missing-compose.yml",
                profile=profile,
                phase_names=("validate",),
                include_coverage=True,
            )


@pytest.mark.unit
async def test_hosted_coverage_uses_profile_enforcement_policy(
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
                        "percent": 87.5,
                        "minimum_percent": 0.0,
                        "enforce": False,
                        "status": "reported",
                        "reason_code": "COVERAGE_OK",
                        "command_result": {
                            "command": "uv run pytest --cov=awf",
                            "returncode": 0,
                            "duration_seconds": 4.2,
                            "stdout": "coverage reported\n",
                            "stderr": "",
                            "phase": "coverage",
                            "reason_code": "COMMAND_SUCCEEDED",
                        },
                    },
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-test",
            "validation": {
                "coverage": {
                    "minimum_percent": 99.0,
                    "enforce": True,
                    "command": "uv run pytest --cov=awf",
                },
                "strategy": {"final_gate": "coverage"},
            },
        }
    )

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
            profile=profile,
        )

    assert result is not None
    assert not result.ok
    assert result.minimum_percent == 99.0
    assert result.enforce is True
    assert result.status == "failed"
    assert result.reason_code == "COVERAGE_BELOW_THRESHOLD"


@pytest.mark.unit
async def test_hosted_coverage_preserves_failure_evidence_fields(
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
                        "percent": 98.5,
                        "minimum_percent": 99.0,
                        "enforce": True,
                        "status": "failed",
                        "reason_code": "PYTEST_TEST_FAILURE",
                        "command_result": {
                            "command": "uv run pytest --cov=awf",
                            "returncode": 1,
                            "duration_seconds": 4.2,
                            "stdout": "FAILED tests/unit/test_widget.py::test_handles_edges\n",
                            "stderr": "",
                            "phase": "coverage",
                            "reason_code": "PYTEST_TEST_FAILURE",
                        },
                        "failing_test_node_ids": ["tests/unit/test_widget.py::test_handles_edges"],
                        "failing_test_evidence": [
                            "FAILED tests/unit/test_widget.py::test_handles_edges"
                        ],
                        "provider_failure_evidence": [
                            "Coverage failure: total of 98.5 is less than fail-under=99"
                        ],
                        "parallel_workers_requested": 20,
                        "parallel_workers_effective": 3,
                        "parallel_distribution": "loadscope",
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
    assert result.failing_test_node_ids == ["tests/unit/test_widget.py::test_handles_edges"]
    assert result.failing_test_evidence == ["FAILED tests/unit/test_widget.py::test_handles_edges"]
    assert result.provider_failure_evidence == [
        "Coverage failure: total of 98.5 is less than fail-under=99"
    ]
    assert result.parallel_workers_requested == 20
    assert result.parallel_workers_effective == 3
    assert result.parallel_distribution == "loadscope"


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
async def test_hosted_coverage_rejects_malformed_coverage_payload(tmp_path: Path) -> None:
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
                    "coverage": "not-a-coverage-object",
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        delegate = HostedValidationDelegate(
            _config(),
            artifacts_dir=tmp_path,
            client=client,
        )
        with pytest.raises(HostedDelegationProtocolError, match="malformed coverage"):
            await delegate.run_profile_coverage(
                workspace_id="ws_hosted",
                compose_project="unused",
                compose_file=tmp_path / "missing-compose.yml",
                profile=WorkspaceProfile(name="hosted-test"),
            )


@pytest.mark.unit
async def test_hosted_coverage_rejects_invalid_optional_percent(tmp_path: Path) -> None:
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
                        "percent": "99.5",
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
        with pytest.raises(HostedDelegationProtocolError, match="invalid float field"):
            await delegate.run_profile_coverage(
                workspace_id="ws_hosted",
                compose_project="unused",
                compose_file=tmp_path / "missing-compose.yml",
                profile=WorkspaceProfile(name="hosted-test"),
            )


@pytest.mark.unit
async def test_hosted_coverage_fails_closed_on_enforced_unexpected_status(
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
                        "status": "error",
                        "reason_code": "COVERAGE_PROVIDER_FAILED",
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
    assert result.status == "failed"
    assert result.reason_code == "COVERAGE_PROVIDER_FAILED"


@pytest.mark.unit
async def test_hosted_coverage_fails_closed_on_unexpected_status_with_ok_reason(
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
                        "status": "error",
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
    assert result.status == "failed"
    assert result.reason_code == "COVERAGE_OK"


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
