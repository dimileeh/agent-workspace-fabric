"""Hosted delegation contract branch tests."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from awf.profiles.models import WorkspaceProfile
from awf.runtime import hosted_delegation as hosted_delegation_mod
from awf.runtime.alembic_validation import (
    ALEMBIC_MIGRATION_POLICY_COMMAND,
    ALEMBIC_MIGRATION_POLICY_PHASE,
)
from awf.runtime.hosted_delegation import (
    HostedDelegationConfig,
    HostedDelegationProtocolError,
    HostedValidationDelegate,
)
from awf.runtime.validation_setup import DB_REFRESH_PHASE


def _config() -> HostedDelegationConfig:
    return HostedDelegationConfig(
        base_url="https://hosted.example.test/api",
        bearer_token="secret-token",
        poll_interval_seconds=0.001,
        operation_timeout_seconds=1.0,
        request_timeout_seconds=1.0,
        cancel_timeout_seconds=1.0,
        max_output_bytes=100_000,
    )


@pytest.mark.unit
async def test_hosted_validation_profile_tool_preflight_noops_without_findings(
    tmp_path: Path,
) -> None:
    """A hosted profile with no static tool findings returns empty validation evidence."""
    delegate = HostedValidationDelegate(_config(), artifacts_dir=tmp_path)

    result = await delegate.run_profile_tool_preflight(
        workspace_id="ws_hosted",
        profile=WorkspaceProfile(name="hosted-no-preflight-findings"),
    )

    assert result.all_passed
    assert result.commands == []


@pytest.mark.unit
def test_hosted_validation_expected_commands_place_healthchecks_before_validate() -> None:
    """Hosted validation keeps local command ordering for alembic, DB, and healthchecks."""
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-validation-plan",
            "database": {"pre_validation_refresh": ["python refresh.py"]},
            "validation": {
                "alembic": {"enabled": True},
                "coverage": {
                    "command": "coverage xml",
                    "minimum_percent": 99.0,
                },
                "healthchecks": [{"name": "runtime", "command": "python health.py"}],
            },
            "phases": {
                "validate": [{"command": "pytest -q", "required": False}],
            },
        }
    )

    commands = hosted_delegation_mod._hosted_validation_expected_commands(
        profile,
        ("validate",),
        run_healthchecks=True,
    )

    assert [(command.phase, command.command, command.required) for command in commands] == [
        (ALEMBIC_MIGRATION_POLICY_PHASE, ALEMBIC_MIGRATION_POLICY_COMMAND, True),
        (DB_REFRESH_PHASE, "python refresh.py", True),
        ("healthcheck", "python health.py", True),
        ("validate", "pytest -q", False),
    ]
    assert (
        hosted_delegation_mod._hosted_validation_expected_command_count(
            profile,
            ("validate",),
            run_healthchecks=True,
        )
        == 4
    )
    assert (
        hosted_delegation_mod._hosted_validation_poll_output_slots(
            profile,
            ("validate",),
            include_coverage=True,
            expected_command_count=len(commands),
        )
        == 6
    )


@pytest.mark.unit
def test_hosted_validation_expected_commands_place_pending_healthchecks_at_edges() -> None:
    """Healthchecks run before non-validate phases and after DB-only validate hooks."""
    setup_profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-setup-healthcheck",
            "validation": {"healthchecks": [{"name": "runtime", "command": "python health.py"}]},
            "phases": {"setup": ["python setup.py"]},
        }
    )
    setup_commands = hosted_delegation_mod._hosted_validation_expected_commands(
        setup_profile,
        ("setup",),
        run_healthchecks=True,
    )
    assert [(command.phase, command.command) for command in setup_commands] == [
        ("healthcheck", "python health.py"),
        ("setup", "python setup.py"),
    ]

    db_only_profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-db-only-healthcheck",
            "database": {"pre_validation_refresh": ["python refresh.py"]},
            "validation": {"healthchecks": [{"name": "runtime", "command": "python health.py"}]},
        }
    )
    db_only_commands = hosted_delegation_mod._hosted_validation_expected_commands(
        db_only_profile,
        ("validate",),
        run_healthchecks=True,
    )
    assert [(command.phase, command.command) for command in db_only_commands] == [
        (DB_REFRESH_PHASE, "python refresh.py"),
        ("healthcheck", "python health.py"),
    ]


@pytest.mark.unit
def test_hosted_delegation_contract_guards_reject_malformed_payload_edges() -> None:
    """Low-level contract helpers fail closed on malformed host responses."""
    expected = hosted_delegation_mod._HostedValidationExpectedCommand(
        phase="validate",
        command="pytest -q",
        required=True,
    )
    with pytest.raises(HostedDelegationProtocolError, match="malformed"):
        hosted_delegation_mod._validate_hosted_validation_command_identity(
            [],
            expected=expected,
        )

    with pytest.raises(HostedDelegationProtocolError, match="missing status"):
        hosted_delegation_mod._coverage_status_from_payload({"status": "  "}, enforce=True)

    assert (
        hosted_delegation_mod._join_url(
            "https://hosted.example.test/api",
            "v1/operations/op_1",
        )
        == "https://hosted.example.test/api/v1/operations/op_1"
    )


@pytest.mark.unit
async def test_hosted_validation_start_http_failure_reraises_without_cancel(
    tmp_path: Path,
) -> None:
    """A failed start response has no operation to cancel and propagates the HTTP error."""
    seen_paths: list[str] = []

    async def _handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        return httpx.Response(503, request=request, text="host unavailable")

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        delegate = HostedValidationDelegate(_config(), artifacts_dir=tmp_path, client=client)
        with pytest.raises(httpx.HTTPStatusError):
            await delegate.run_profile_phases(
                workspace_id="ws_hosted",
                compose_project="unused",
                compose_file=tmp_path / "missing-compose.yml",
                profile=WorkspaceProfile(name="hosted-test"),
                phase_names=("validate",),
            )

    assert seen_paths == ["/api/v1/validation-runs"]


@pytest.mark.unit
async def test_hosted_validation_setup_materializes_playwright_browser_install_in_payload_and_accepts_evidence(
    tmp_path: Path,
) -> None:
    """Hosted setup with runtime.browsers sends both setup commands and accepts evidence."""
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-playwright-setup",
            "runtime": {"browsers": ["chromium"]},
            "phases": {"setup": ["npm ci"]},
        }
    )
    posted_payload: dict[str, object] | None = None

    async def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal posted_payload
        if request.method == "POST" and request.url.path == "/api/v1/validation-runs":
            posted_payload = json.loads(request.content)
            return httpx.Response(
                202,
                json={
                    "operation_id": "val_playwright",
                    "workspace_id": "ws_hosted",
                    "operation_url": "/v1/operations/val_playwright",
                },
            )
        if request.method == "GET" and request.url.path == "/v1/operations/val_playwright":
            return httpx.Response(
                200,
                json={
                    "operation_id": "val_playwright",
                    "workspace_id": "ws_hosted",
                    "state": "succeeded",
                    "commands": [
                        {
                            "command": "npm ci",
                            "returncode": 0,
                            "duration_seconds": 1.0,
                            "stdout": "",
                            "stderr": "",
                            "phase": "setup",
                        },
                        {
                            "command": "npx playwright install chromium",
                            "returncode": 0,
                            "duration_seconds": 2.0,
                            "stdout": "",
                            "stderr": "",
                            "phase": "setup",
                        },
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
            phase_names=("setup",),
            include_coverage=False,
        )

    assert posted_payload is not None
    setup_commands = [
        item["command"]
        for item in posted_payload["profile"]["phases"]["setup"]  # type: ignore[index]
    ]
    assert setup_commands == ["npm ci", "npx playwright install chromium"]
    assert result.all_passed
    assert len(result.commands) == 2


@pytest.mark.unit
async def test_hosted_validation_coverage_payload_excludes_playwright_browser_install(
    tmp_path: Path,
) -> None:
    """Coverage-only hosted validation must not materialize setup browser-install."""
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-coverage-playwright",
            "runtime": {"browsers": ["chromium"]},
            "phases": {"setup": ["npm ci"]},
            "validation": {
                "coverage": {
                    "minimum_percent": 1,
                    "command": "pytest --cov",
                },
            },
        }
    )
    posted_payload: dict[str, object] | None = None

    async def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal posted_payload
        if request.method == "POST" and request.url.path == "/api/v1/validation-runs":
            posted_payload = json.loads(request.content)
            return httpx.Response(
                202,
                json={
                    "operation_id": "val_coverage",
                    "workspace_id": "ws_hosted",
                    "operation_url": "/v1/operations/val_coverage",
                },
            )
        if request.method == "GET" and request.url.path == "/v1/operations/val_coverage":
            return httpx.Response(
                200,
                json={
                    "operation_id": "val_coverage",
                    "workspace_id": "ws_hosted",
                    "state": "succeeded",
                    "coverage": {
                        "provider": "pytest",
                        "percent": 99.0,
                        "minimum_percent": 1.0,
                        "enforce": False,
                        "status": "passed",
                        "reason_code": "COVERAGE_OK",
                        "command_result": {
                            "command": "pytest --cov",
                            "returncode": 0,
                            "duration_seconds": 1.0,
                            "stdout": "",
                            "stderr": "",
                            "phase": "coverage",
                        },
                    },
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        delegate = HostedValidationDelegate(_config(), artifacts_dir=tmp_path, client=client)
        await delegate.run_profile_coverage(
            workspace_id="ws_hosted",
            compose_project="unused",
            compose_file=tmp_path / "missing-compose.yml",
            profile=profile,
        )

    assert posted_payload is not None
    setup_commands = [
        item["command"]
        for item in posted_payload["profile"]["phases"]["setup"]  # type: ignore[index]
    ]
    assert setup_commands == ["npm ci"]
