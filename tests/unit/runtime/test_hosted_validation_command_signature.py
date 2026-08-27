"""Hosted validation command_signature contract tests."""

from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from awf.profiles.models import WorkspaceProfile
from awf.runtime import hosted_delegation as hosted_delegation_mod
from awf.runtime.hosted_delegation import (
    HostedDelegationConfig,
    HostedDelegationProtocolError,
    HostedValidationDelegate,
)


def _expected_signature(phase: str, command: str) -> str:
    """Mirror the hosted command signature algorithm used by Core."""
    payload = json.dumps([phase, command], ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


@pytest.mark.unit
def test_hosted_validation_command_signature_fixed_vector() -> None:
    """Lock the shared Core/awf-cloud signature algorithm with a non-ASCII command."""
    phase = "validate"
    command = "npm tést — ci"
    expected_payload = '["validate","npm tést — ci"]'
    assert (
        json.dumps([phase, command], ensure_ascii=False, separators=(",", ":")) == expected_payload
    )
    assert hosted_delegation_mod._hosted_validation_command_signature(
        phase,
        command,
    ) == ("sha256:bc85aa4429e7a9e39a048f6f60d5175b5c4f34e2f8bd07f55c789c3d1c4ddea6")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("signature", "expected"),
    [
        ("sha256:ABCDEF0123456789abcdef0123456789abcdef0123456789abcdef0123456789", False),
        ("sha256:short", False),
        ("md5:0123456789abcdef0123456789abcdef", False),
        ("", False),
        (123, False),  # type: ignore[arg-type]
        (
            "sha256:bc85aa4429e7a9e39a048f6f60d5175b5c4f34e2f8bd07f55c789c3d1c4ddea6",
            True,
        ),
    ],
)
def test_hosted_validation_command_signature_rejects_malformed(
    signature: object,
    *,
    expected: bool,
) -> None:
    """Reject signatures that are not well-formed SHA-256 hosted command identities."""
    assert (
        hosted_delegation_mod._hosted_validation_command_signature_is_well_formed(signature)
        is expected
    )


def _expected_command(
    *,
    phase: str = "validate",
    command: str = "pytest -q",
    required: bool = True,
) -> hosted_delegation_mod._HostedValidationExpectedCommand:
    """Build an expected hosted validation command with a matching signature."""
    return hosted_delegation_mod._HostedValidationExpectedCommand(
        phase=phase,
        command=command,
        required=required,
        command_signature=_expected_signature(phase, command),
    )


@pytest.mark.unit
def test_hosted_validation_command_identity_accepts_redacted_with_valid_signature() -> None:
    """Accept redacted command text when the signature matches the expected command."""
    expected = _expected_command(command="echo $SECRET")
    hosted_delegation_mod._validate_hosted_validation_command_identity(
        {
            "phase": "validate",
            "command": "[REDACTED]",
            "command_signature": expected.command_signature,
        },
        expected=expected,
    )


@pytest.mark.unit
def test_hosted_validation_command_identity_rejects_mismatch_signature() -> None:
    """Reject evidence when the signature does not match the expected command."""
    expected = _expected_command(command="echo $SECRET")
    with pytest.raises(HostedDelegationProtocolError, match="command signature mismatch"):
        hosted_delegation_mod._validate_hosted_validation_command_identity(
            {
                "phase": "validate",
                "command": "[REDACTED]",
                "command_signature": _expected_signature("validate", "echo other"),
            },
            expected=expected,
        )


@pytest.mark.unit
def test_hosted_validation_command_identity_rejects_malformed_signature() -> None:
    """Reject evidence when the command signature is present but malformed."""
    expected = _expected_command(command="echo $SECRET")
    with pytest.raises(HostedDelegationProtocolError, match="command signature is malformed"):
        hosted_delegation_mod._validate_hosted_validation_command_identity(
            {
                "phase": "validate",
                "command": "[REDACTED]",
                "command_signature": "sha256:NOT_VALID",
            },
            expected=expected,
        )


@pytest.mark.unit
@pytest.mark.parametrize("command_signature", ["", None])
def test_hosted_validation_command_identity_rejects_present_empty_signature(
    command_signature: object,
) -> None:
    """Reject empty or null signatures when the signature field is present."""
    expected = _expected_command(command="pytest -q")
    with pytest.raises(HostedDelegationProtocolError, match="command signature is malformed"):
        hosted_delegation_mod._validate_hosted_validation_command_identity(
            {
                "phase": "validate",
                "command": "pytest -q",
                "command_signature": command_signature,
            },
            expected=expected,
        )


@pytest.mark.unit
def test_hosted_validation_command_identity_legacy_exact_command_match() -> None:
    """Keep legacy exact-command matching when no signature is provided."""
    expected = hosted_delegation_mod._HostedValidationExpectedCommand(
        phase="validate",
        command="pytest -q",
        required=True,
        command_signature=_expected_signature("validate", "pytest -q"),
    )
    hosted_delegation_mod._validate_hosted_validation_command_identity(
        {"phase": "validate", "command": "pytest -q"},
        expected=expected,
    )


@pytest.mark.unit
def test_hosted_validation_command_identity_legacy_redacted_fails_without_signature() -> None:
    """Reject redacted command text when legacy hosts omit command signatures."""
    expected = _expected_command(command="echo $SECRET")
    with pytest.raises(HostedDelegationProtocolError, match="command identity mismatch"):
        hosted_delegation_mod._validate_hosted_validation_command_identity(
            {"phase": "validate", "command": "[REDACTED]"},
            expected=expected,
        )


@pytest.mark.unit
def test_hosted_validation_command_identity_rejects_wrong_phase_even_with_signature() -> None:
    """Reject evidence when the phase does not match even with a valid signature."""
    expected = _expected_command(phase="validate", command="pytest -q")
    with pytest.raises(HostedDelegationProtocolError, match="command identity mismatch"):
        hosted_delegation_mod._validate_hosted_validation_command_identity(
            {
                "phase": "post_agent",
                "command": "[REDACTED]",
                "command_signature": expected.command_signature,
            },
            expected=expected,
        )


@pytest.mark.unit
def test_hosted_validation_expected_commands_include_command_signature() -> None:
    """Attach command signatures to every expected hosted validation command."""
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-signature-expected",
            "phases": {"validate": [{"command": "pytest -q", "required": True}]},
        }
    )
    commands = hosted_delegation_mod._hosted_validation_expected_commands(
        profile,
        ("validate",),
        run_healthchecks=False,
    )
    assert len(commands) == 1
    assert commands[0].command_signature == _expected_signature("validate", "pytest -q")


def _config() -> HostedDelegationConfig:
    """Return a hosted delegation config for signature contract tests."""
    return HostedDelegationConfig(
        base_url="https://hosted.example.test",
        bearer_token="secret-token",
        poll_interval_seconds=0.001,
        operation_timeout_seconds=1.0,
        request_timeout_seconds=1.0,
        cancel_timeout_seconds=1.0,
        max_output_bytes=100_000,
    )


@pytest.mark.unit
async def test_hosted_validation_delegate_uses_expected_command_after_signature_auth(
    tmp_path,
) -> None:
    """A valid signature must not retain host-provided command text that may embed secrets."""
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-signature-sanitize",
            "phases": {"validate": [{"command": "echo $SECRET", "required": True}]},
        }
    )
    expected_commands = hosted_delegation_mod._hosted_validation_expected_commands(
        profile,
        ("validate",),
        run_healthchecks=False,
    )
    expected = expected_commands[0]
    leaky_command = "echo resolved-secret-token"

    async def _handler(request: httpx.Request) -> httpx.Response:
        """Return leaky command text that must be replaced after signature auth."""
        if request.method == "POST" and request.url.path == "/v1/validation-runs":
            return httpx.Response(
                202,
                json={
                    "operation_id": "val_sig_sanitize",
                    "workspace_id": "ws_hosted",
                    "operation_url": "/v1/operations/val_sig_sanitize",
                },
            )
        if request.method == "GET" and request.url.path == "/v1/operations/val_sig_sanitize":
            return httpx.Response(
                200,
                json={
                    "operation_id": "val_sig_sanitize",
                    "workspace_id": "ws_hosted",
                    "state": "failed",
                    "commands": [
                        {
                            "command": leaky_command,
                            "returncode": 1,
                            "duration_seconds": 0.2,
                            "stdout": "",
                            "stderr": "boom",
                            "phase": expected.phase,
                            "command_signature": expected.command_signature,
                        }
                    ],
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        delegate = HostedValidationDelegate(_config(), artifacts_dir=tmp_path, client=client)
        result = await delegate.run_profile_phases(
            workspace_id="ws_hosted",
            compose_project="unused",
            compose_file=tmp_path / "missing-compose.yml",
            profile=profile,
            phase_names=("validate",),
        )

    assert not result.all_passed
    assert len(result.commands) == 1
    assert result.commands[0].command == expected.command
    assert result.commands[0].command != leaky_command


@pytest.mark.unit
async def test_hosted_validation_delegate_rejects_extra_command_evidence(
    tmp_path,
) -> None:
    """Extra commands beyond the expected list must not bypass signature auth."""
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-signature-extra",
            "phases": {"validate": [{"command": "pytest -q", "required": True}]},
        }
    )
    expected_commands = hosted_delegation_mod._hosted_validation_expected_commands(
        profile,
        ("validate",),
        run_healthchecks=False,
    )
    expected = expected_commands[0]

    async def _handler(request: httpx.Request) -> httpx.Response:
        """Return one valid command plus an unexpected extra evidence row."""
        if request.method == "POST" and request.url.path == "/v1/validation-runs":
            return httpx.Response(
                202,
                json={
                    "operation_id": "val_sig_extra",
                    "workspace_id": "ws_hosted",
                    "operation_url": "/v1/operations/val_sig_extra",
                },
            )
        if request.method == "GET" and request.url.path == "/v1/operations/val_sig_extra":
            return httpx.Response(
                200,
                json={
                    "operation_id": "val_sig_extra",
                    "workspace_id": "ws_hosted",
                    "state": "failed",
                    "commands": [
                        {
                            "command": expected.command,
                            "returncode": 0,
                            "duration_seconds": 0.2,
                            "stdout": "",
                            "stderr": "",
                            "phase": expected.phase,
                            "command_signature": expected.command_signature,
                        },
                        {
                            "command": "echo resolved-secret-token",
                            "returncode": 1,
                            "duration_seconds": 0.1,
                            "stdout": "",
                            "stderr": "boom",
                            "phase": "validate",
                            "required": True,
                        },
                    ],
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        delegate = HostedValidationDelegate(_config(), artifacts_dir=tmp_path, client=client)
        with pytest.raises(
            HostedDelegationProtocolError,
            match="unexpected extra commands",
        ):
            await delegate.run_profile_phases(
                workspace_id="ws_hosted",
                compose_project="unused",
                compose_file=tmp_path / "missing-compose.yml",
                profile=profile,
                phase_names=("validate",),
            )


@pytest.mark.unit
async def test_hosted_validation_delegate_rejects_swapped_signature_evidence(
    tmp_path,
) -> None:
    """Reject command evidence when signatures are present but out of order."""
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-signature-order",
            "phases": {
                "validate": [
                    {"command": "advisory-lint", "required": False},
                    {"command": "pytest -q", "required": True},
                ]
            },
        }
    )
    expected_commands = hosted_delegation_mod._hosted_validation_expected_commands(
        profile,
        ("validate",),
        run_healthchecks=False,
    )
    swapped = list(reversed(expected_commands))

    async def _handler(request: httpx.Request) -> httpx.Response:
        """Return signed commands in reverse order to trigger identity rejection."""
        if request.method == "POST" and request.url.path == "/v1/validation-runs":
            return httpx.Response(
                202,
                json={
                    "operation_id": "val_sig_swap",
                    "workspace_id": "ws_hosted",
                    "operation_url": "/v1/operations/val_sig_swap",
                },
            )
        if request.method == "GET" and request.url.path == "/v1/operations/val_sig_swap":
            return httpx.Response(
                200,
                json={
                    "operation_id": "val_sig_swap",
                    "workspace_id": "ws_hosted",
                    "state": "succeeded",
                    "commands": [
                        {
                            "command": "[REDACTED]",
                            "returncode": 0,
                            "duration_seconds": 0.2,
                            "stdout": "",
                            "stderr": "",
                            "phase": command.phase,
                            "command_signature": command.command_signature,
                        }
                        for command in swapped
                    ],
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        delegate = HostedValidationDelegate(_config(), artifacts_dir=tmp_path, client=client)
        with pytest.raises(HostedDelegationProtocolError, match="command signature mismatch"):
            await delegate.run_profile_phases(
                workspace_id="ws_hosted",
                compose_project="unused",
                compose_file=tmp_path / "missing-compose.yml",
                profile=profile,
                phase_names=("validate",),
            )


@pytest.mark.unit
def test_hosted_coverage_result_rejects_mismatched_command_identity(tmp_path) -> None:
    """Reject coverage command evidence when it does not match the profile command."""
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-coverage-identity",
            "validation": {
                "coverage": {
                    "minimum_percent": 99.0,
                    "command": "uv run pytest --cov=awf",
                },
            },
        }
    )
    coverage_policy = profile.validation.coverage
    with pytest.raises(HostedDelegationProtocolError, match="command identity mismatch"):
        hosted_delegation_mod._coverage_result_from_payload(
            {
                "provider": "python",
                "percent": 99.5,
                "minimum_percent": 99.0,
                "enforce": True,
                "status": "passed",
                "reason_code": "COVERAGE_OK",
                "command_result": {
                    "command": "echo leaked-secret",
                    "returncode": 0,
                    "duration_seconds": 1.0,
                    "stdout": "",
                    "stderr": "",
                    "phase": "coverage",
                    "reason_code": "COMMAND_SUCCEEDED",
                },
            },
            artifacts_dir=tmp_path,
            max_output_bytes=100_000,
            coverage_policy=coverage_policy,
        )


@pytest.mark.unit
def test_hosted_coverage_result_uses_expected_command_after_signature_auth(tmp_path) -> None:
    """Coverage command evidence must not retain host-provided command text after auth."""
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-coverage-signature",
            "validation": {
                "coverage": {
                    "minimum_percent": 99.0,
                    "command": "echo $SECRET",
                },
            },
        }
    )
    coverage_policy = profile.validation.coverage
    expected = hosted_delegation_mod._hosted_coverage_expected_command(coverage_policy)
    assert expected is not None
    leaky_command = "echo resolved-secret-token"
    result = hosted_delegation_mod._coverage_result_from_payload(
        {
            "provider": "python",
            "percent": 99.5,
            "minimum_percent": 99.0,
            "enforce": True,
            "status": "passed",
            "reason_code": "COVERAGE_OK",
            "command_result": {
                "command": leaky_command,
                "returncode": 0,
                "duration_seconds": 1.0,
                "stdout": "",
                "stderr": "",
                "phase": "coverage",
                "command_signature": expected.command_signature,
                "reason_code": "COMMAND_SUCCEEDED",
            },
        },
        artifacts_dir=tmp_path,
        max_output_bytes=100_000,
        coverage_policy=coverage_policy,
    )
    assert result.command_result is not None
    assert result.command_result.command == expected.command
    assert result.command_result.command != leaky_command


@pytest.mark.unit
def test_hosted_coverage_result_accepts_baseline_coverage_phase(tmp_path) -> None:
    """Baseline coverage uses phase baseline_coverage, not coverage."""
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-baseline-coverage-identity",
            "validation": {
                "coverage": {
                    "minimum_percent": 99.0,
                    "command": "uv run pytest --cov=awf",
                },
            },
        }
    )
    coverage_policy = profile.validation.coverage
    expected = hosted_delegation_mod._hosted_coverage_expected_command(
        coverage_policy,
        phase="baseline_coverage",
    )
    assert expected is not None
    assert expected.phase == "baseline_coverage"
    result = hosted_delegation_mod._coverage_result_from_payload(
        {
            "provider": "python",
            "percent": 99.5,
            "minimum_percent": 99.0,
            "enforce": True,
            "status": "passed",
            "reason_code": "COVERAGE_OK",
            "command_result": {
                "command": coverage_policy.command.command,
                "returncode": 0,
                "duration_seconds": 1.0,
                "stdout": "",
                "stderr": "",
                "phase": "baseline_coverage",
                "command_signature": expected.command_signature,
                "reason_code": "COMMAND_SUCCEEDED",
            },
        },
        artifacts_dir=tmp_path,
        max_output_bytes=100_000,
        coverage_policy=coverage_policy,
        coverage_phase="baseline_coverage",
    )
    assert result.command_result is not None
    assert result.command_result.command == expected.command


@pytest.mark.unit
async def test_hosted_coverage_delegate_rejects_mismatched_command_signature(
    tmp_path,
) -> None:
    """Coverage-only hosted validation must reject mismatched command signatures."""
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-coverage-signature-reject",
            "validation": {
                "coverage": {
                    "minimum_percent": 99.0,
                    "command": "echo $SECRET",
                },
                "strategy": {"final_gate": "coverage"},
            },
        }
    )
    wrong_signature = _expected_signature("coverage", "echo other")

    async def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/validation-runs":
            return httpx.Response(
                202,
                json={
                    "operation_id": "cov_sig_mismatch",
                    "workspace_id": "ws_hosted",
                    "operation_url": "/v1/operations/cov_sig_mismatch",
                },
            )
        if request.method == "GET" and request.url.path == "/v1/operations/cov_sig_mismatch":
            return httpx.Response(
                200,
                json={
                    "operation_id": "cov_sig_mismatch",
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
                            "command": "[REDACTED]",
                            "returncode": 0,
                            "duration_seconds": 1.0,
                            "stdout": "",
                            "stderr": "",
                            "phase": "coverage",
                            "command_signature": wrong_signature,
                            "reason_code": "COMMAND_SUCCEEDED",
                        },
                    },
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        delegate = HostedValidationDelegate(_config(), artifacts_dir=tmp_path, client=client)
        with pytest.raises(HostedDelegationProtocolError, match="command signature mismatch"):
            await delegate.run_profile_coverage(
                workspace_id="ws_hosted",
                compose_project="unused",
                compose_file=tmp_path / "missing-compose.yml",
                profile=profile,
            )
