"""Contract tests for safe control operations across REST, CLI, and parity docs.

These tests define explicit request/output/error expectations for the six control
operations that are canonical in REST/API and exposed in CLI: cancel, stop,
Destroy, refresh, validate, and rebase.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from typer.testing import CliRunner

from awf.api.schemas import OperationResponse
from awf.cli import common as cli_common
from awf.cli.main import app
from tests.unit.mcp._parity_utils import _parity_rows, _strip_backticks

_RUNNER = CliRunner()
_OPERATION_CREATED_AT = datetime(2026, 1, 1, tzinfo=UTC)
_CONTROL_RESPONSE_FIELDS = (
    "workspace_id",
    "operation_id",
    "operation_status",
    "status",
    "message",
    "warnings",
)
_OPERATION_RESPONSE_FIELDS = (
    "id",
    "workspace_id",
    "type",
    "status",
    "payload",
    "created_at",
)


@pytest.fixture(autouse=True)
def _isolate_cli_local_service_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep default CLI URL contracts independent from a developer root `.env`."""

    monkeypatch.setattr(cli_common, "local_service_environ", lambda _environ: {})


def _operation_response_payload(
    *,
    operation_id: str,
    workspace_id: str,
    operation_type: str,
    status: str = "pending",
    idempotency_key: str,
    reason: str,
    reason_code: str,
    expected_version: int,
    extra_payload: dict[str, object | None] | None = None,
) -> dict[str, Any]:
    payload: dict[str, object | None] = {
        "owner": "operator_api",
        "source": "operator_api",
        "reason": reason,
        "reason_code": reason_code,
        "requested_action": operation_type,
        "expected_version": expected_version,
    }
    if extra_payload is not None:
        payload.update(extra_payload)
    return OperationResponse(
        id=operation_id,
        workspace_id=workspace_id,
        type=operation_type,
        status=status,
        error_code=None,
        error_message=None,
        payload=payload,
        result=None,
        idempotency_key=idempotency_key,
        created_at=_OPERATION_CREATED_AT,
        started_at=None,
        finished_at=None,
    ).model_dump(mode="json")


def _mock_response(
    *, status_code: int = 200, payload: object | None = None, text: str = ""
) -> MagicMock:
    response = MagicMock(spec=httpx.Response)
    response.status_code = status_code
    response.content = b"ok" if payload is not None or text else b""
    response.text = text or (json.dumps(payload) if payload is not None else "")
    response.json.return_value = payload
    return response


def _assert_control_headers(
    headers: dict[str, str],
    *,
    idempotency_key: str,
    if_match: str | None,
) -> None:
    assert headers["Idempotency-Key"] == idempotency_key
    if if_match is None:
        assert "If-Match" not in headers
    else:
        assert headers["If-Match"] == if_match


@dataclass(frozen=True)
class _ControlCase:
    capability: str
    command: str
    command_args: tuple[str, ...]
    workspace_id: str
    idempotency_key: str
    if_match: str
    method: str
    rest_path: str
    expected_body: dict[str, Any] | None
    expected_query: dict[str, Any] | None
    success_status: int
    response_payload: dict[str, Any]
    mcp_tool: str | None
    matrix_status: str
    forbidden_error_code: str
    parity_rest_path: str
    parity_cli: str
    missing_mcp_tool: str | None = None
    response_fields: tuple[str, ...] = _CONTROL_RESPONSE_FIELDS


_CONTROL_CASES: tuple[_ControlCase, ...] = (
    _ControlCase(
        capability="Cancel workspace",
        command="cancel",
        command_args=(
            "--reason",
            "operator requested",
            "--no-stop-stack",
        ),
        workspace_id="ws_cancel",
        idempotency_key="cancel-key",
        if_match="7",
        method="POST",
        rest_path="/v1/workspaces/{workspace_id}/cancel",
        expected_body={"reason": "operator requested", "stop_stack": False},
        expected_query=None,
        success_status=200,
        response_payload={
            "workspace_id": "ws_cancel",
            "operation_id": "op_cancel",
            "operation_status": "succeeded",
            "status": "cancelling",
            "message": "workspace cancellation requested",
            "warnings": [],
        },
        mcp_tool="awf_cancel_workspace",
        matrix_status="MCP implemented",
        forbidden_error_code="VERSION_CONFLICT",
        parity_rest_path="/v1/workspaces/{workspace_id}/cancel",
        parity_cli="awf workspace cancel",
    ),
    _ControlCase(
        capability="Stop workspace stack",
        command="stop",
        command_args=(
            "--reason",
            "stack unstable",
        ),
        workspace_id="ws_stop",
        idempotency_key="stop-key",
        if_match="13",
        method="POST",
        rest_path="/v1/workspaces/{workspace_id}/stop",
        expected_body={"reason": "stack unstable"},
        expected_query=None,
        success_status=200,
        response_payload={
            "workspace_id": "ws_stop",
            "operation_id": "op_stop",
            "operation_status": "succeeded",
            "status": "stopping",
            "message": "workspace stop requested",
            "warnings": [],
        },
        mcp_tool="awf_stop_workspace",
        matrix_status="MCP implemented",
        forbidden_error_code="VERSION_CONFLICT",
        parity_rest_path="/v1/workspaces/{workspace_id}/stop",
        parity_cli="awf workspace stop",
    ),
    _ControlCase(
        capability="Destroy workspace resources",
        command="destroy",
        command_args=(
            "--force",
            "--no-remove-volumes",
            "--no-remove-worktree",
        ),
        workspace_id="ws_destroy",
        idempotency_key="destroy-key",
        if_match="19",
        method="DELETE",
        rest_path="/v1/workspaces/{workspace_id}",
        expected_body=None,
        expected_query={"force": True, "remove_volumes": False, "remove_worktree": False},
        success_status=200,
        response_payload={
            "workspace_id": "ws_destroy",
            "operation_id": "op_destroy",
            "operation_status": "succeeded",
            "status": "destroying",
            "message": "workspace destruction requested",
            "warnings": [],
        },
        mcp_tool="awf_destroy_workspace",
        matrix_status="MCP implemented",
        forbidden_error_code="WORKSPACE_ACTIVE",
        parity_rest_path="/v1/workspaces/{workspace_id}",
        parity_cli="awf workspace destroy",
    ),
    _ControlCase(
        capability="Refresh workspace",
        command="refresh",
        command_args=(
            "--reason",
            "stale branch",
        ),
        workspace_id="ws_refresh",
        idempotency_key="refresh-key",
        if_match="33",
        method="POST",
        rest_path="/v1/workspaces/{workspace_id}/refresh",
        expected_body={"reason": "stale branch"},
        expected_query=None,
        success_status=202,
        response_payload=_operation_response_payload(
            operation_id="op_refresh",
            workspace_id="ws_refresh",
            operation_type="refresh",
            idempotency_key="refresh-key",
            reason="stale branch",
            reason_code="OPERATOR_REFRESH",
            expected_version=33,
        ),
        mcp_tool="awf_refresh_workspace",
        matrix_status="MCP implemented",
        forbidden_error_code="WORKSPACE_STATE_NOT_REFRESHABLE",
        parity_rest_path="/v1/workspaces/{workspace_id}/refresh",
        parity_cli="awf workspace refresh",
        response_fields=_OPERATION_RESPONSE_FIELDS,
    ),
    _ControlCase(
        capability="Request validation",
        command="validate",
        command_args=(
            "--requested-tier",
            "2",
            "--reason",
            "manual revalidation",
        ),
        workspace_id="ws_validate",
        idempotency_key="validate-key",
        if_match="2",
        method="POST",
        rest_path="/v1/workspaces/{workspace_id}/validate",
        expected_body={"reason": "manual revalidation", "requested_tier": 2},
        expected_query=None,
        success_status=202,
        response_payload=_operation_response_payload(
            operation_id="op_validate",
            workspace_id="ws_validate",
            operation_type="validate",
            idempotency_key="validate-key",
            reason="manual revalidation",
            reason_code="OPERATOR_VALIDATE",
            expected_version=2,
            extra_payload={"recovery_mode": "validate_only", "requested_tier": 2},
        ),
        mcp_tool="awf_request_workspace_validation",
        matrix_status="MCP implemented",
        forbidden_error_code="WORKSPACE_STATE_NOT_VALIDATABLE",
        parity_rest_path="/v1/workspaces/{workspace_id}/validate",
        parity_cli="awf workspace validate",
        response_fields=_OPERATION_RESPONSE_FIELDS,
    ),
    _ControlCase(
        capability="Rebase workspace",
        command="rebase",
        command_args=(
            "--reason",
            "recover merge conflicts",
        ),
        workspace_id="ws_rebase",
        idempotency_key="rebase-key",
        if_match="11",
        method="POST",
        rest_path="/v1/workspaces/{workspace_id}/rebase",
        expected_body={"reason": "recover merge conflicts"},
        expected_query=None,
        success_status=202,
        response_payload=_operation_response_payload(
            operation_id="op_rebase",
            workspace_id="ws_rebase",
            operation_type="rebase",
            idempotency_key="rebase-key",
            reason="recover merge conflicts",
            reason_code="OPERATOR_REBASE",
            expected_version=11,
            extra_payload={"recovery_mode": "rebase_only"},
        ),
        mcp_tool="awf_rebase_workspace",
        matrix_status="MCP implemented",
        forbidden_error_code="WORKSPACE_STATE_NOT_REBASEABLE",
        parity_rest_path="/v1/workspaces/{workspace_id}/rebase",
        parity_cli="awf workspace rebase",
        response_fields=_OPERATION_RESPONSE_FIELDS,
    ),
)


def _row_for_capability(capability: str) -> dict[str, str]:
    rows = _parity_rows()
    for row in rows:
        if row.get("Capability", "").strip() == capability:
            return row
    raise AssertionError(f"Missing parity row for {capability}")


def _build_case_args(
    case: _ControlCase,
    *,
    idempotency_key: str | None = "__auto__",
    if_match: str | None = "__auto__",
    include_if_match: bool = True,
) -> list[str]:
    args = ["workspace", case.command, case.workspace_id, *case.command_args]
    if idempotency_key == "__auto__":
        idempotency_key = case.idempotency_key
    if idempotency_key is not None:
        args.extend(["--idempotency-key", idempotency_key])
    if include_if_match:
        if if_match == "__auto__":
            if_match = case.if_match
        if if_match is not None:
            args.extend(["--if-match", if_match])
    return args


def _expected_rest_path(case: _ControlCase) -> str:
    return case.rest_path.format(workspace_id=case.workspace_id)


@pytest.mark.parametrize("case", _CONTROL_CASES)
def test_safe_control_matrix_rows_cover_cli_and_api_contracts(case: _ControlCase) -> None:
    row = _row_for_capability(case.capability)
    cli_cell = _strip_backticks(row.get("CLI surface", ""))
    assert case.parity_cli in cli_cell, case
    expected_rest = f"{case.method} {case.parity_rest_path}"
    parity_rest = _strip_backticks(row.get("Canonical REST surface", ""))
    assert expected_rest in parity_rest, case

    mcp_cell = _strip_backticks(row.get("MCP tool name", "")).strip()
    status = row.get("Status", "").strip()
    assert status == case.matrix_status, case
    if case.mcp_tool is None:
        expected = f"No {case.missing_mcp_tool}"
        assert expected == mcp_cell, case
    else:
        assert case.mcp_tool in mcp_cell, case
    schema_contract = _strip_backticks(row.get("Schema / Error-Code Contract", ""))
    assert "NOT_FOUND" in schema_contract, case
    assert "IDEMPOTENCY_CONFLICT" in schema_contract, case
    assert case.forbidden_error_code in schema_contract, case


@pytest.mark.parametrize("case", _CONTROL_CASES)
def test_control_commands_emit_expected_request_shape_and_output(case: _ControlCase) -> None:
    response = _mock_response(status_code=case.success_status, payload=case.response_payload)
    with patch("awf.cli.main.httpx.request", return_value=response) as mock:
        result = _RUNNER.invoke(app, _build_case_args(case))

    assert result.exit_code == 0, result.output
    assert mock.call_args[0] == (case.method, f"http://localhost:8000{_expected_rest_path(case)}")
    payload = mock.call_args.kwargs
    _assert_control_headers(
        payload["headers"],
        idempotency_key=case.idempotency_key,
        if_match=case.if_match,
    )
    if case.expected_body is None:
        assert "json" not in payload
    else:
        assert payload["json"] == case.expected_body
    if case.expected_query is None:
        assert "params" not in payload
    else:
        assert payload["params"] == case.expected_query

    output = json.loads(result.stdout)
    for field in case.response_fields:
        assert field in output
    assert output == case.response_payload


@pytest.mark.parametrize("case", _CONTROL_CASES)
def test_control_commands_generate_idempotency_key_when_omitted(case: _ControlCase) -> None:
    args = _build_case_args(case, idempotency_key=None)
    response = _mock_response(status_code=case.success_status, payload=case.response_payload)
    with patch("awf.cli.main.httpx.request", return_value=response) as mock:
        result = _RUNNER.invoke(app, args)

    assert result.exit_code == 0, result.output
    generated_key = mock.call_args.kwargs["headers"]["Idempotency-Key"]
    assert re.fullmatch(rf"awf-cli-{case.command}-[0-9a-f]{{32}}", generated_key)


@pytest.mark.parametrize("case", _CONTROL_CASES)
def test_control_commands_surface_invalid_if_match_api_error(case: _ControlCase) -> None:
    args = _build_case_args(case, if_match="bad")
    response = _mock_response(
        status_code=400,
        payload={
            "error_code": "INVALID_REQUEST",
            "message": "If-Match must be a workspace version integer.",
        },
    )
    with patch("awf.cli.main.httpx.request", return_value=response) as mock:
        result = _RUNNER.invoke(app, args)

    assert result.exit_code == 1, result.output
    assert "INVALID_REQUEST" in result.stderr
    assert mock.call_args.kwargs["headers"]["If-Match"] == "bad"


@pytest.mark.parametrize("case", _CONTROL_CASES)
@pytest.mark.parametrize("if_match", ['"7"', 'W/"7"'])
def test_control_commands_forward_etag_if_match_syntax(case: _ControlCase, if_match: str) -> None:
    response = _mock_response(status_code=case.success_status, payload=case.response_payload)
    with patch("awf.cli.main.httpx.request", return_value=response) as mock:
        result = _RUNNER.invoke(app, _build_case_args(case, if_match=if_match))

    assert result.exit_code == 0, result.output
    assert mock.call_args.kwargs["headers"]["If-Match"] == if_match


@pytest.mark.parametrize("case", _CONTROL_CASES)
def test_control_command_invalid_idempotency_key_is_surface_aware(case: _ControlCase) -> None:
    response = _mock_response(
        status_code=400,
        payload={
            "error_code": "INVALID_REQUEST",
            "message": "Idempotency-Key header is required for this endpoint.",
        },
    )
    with patch("awf.cli.main.httpx.request", return_value=response) as mock:
        result = _RUNNER.invoke(app, _build_case_args(case, idempotency_key=" "))

    assert result.exit_code == 1, result.output
    assert "INVALID_REQUEST" in result.stderr
    mock.assert_called_once()


@pytest.mark.parametrize("case", _CONTROL_CASES)
def test_control_command_sends_authorization_header_when_env_token_present(
    case: _ControlCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWF_API_TOKEN", "control-test-token")
    response = _mock_response(status_code=case.success_status, payload=case.response_payload)
    with patch("awf.cli.main.httpx.request", return_value=response) as mock:
        result = _RUNNER.invoke(app, _build_case_args(case))

    assert result.exit_code == 0, result.output
    assert mock.call_args.kwargs["headers"]["Authorization"] == "Bearer control-test-token", case


@pytest.mark.parametrize("case", _CONTROL_CASES)
def test_control_command_does_not_leak_auth_token(
    case: _ControlCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "control-test-token"
    monkeypatch.setenv("AWF_API_TOKEN", token)
    response = _mock_response(status_code=case.success_status, payload=case.response_payload)
    with patch("awf.cli.main.httpx.request", return_value=response) as mock:
        result = _RUNNER.invoke(app, _build_case_args(case))

    assert result.exit_code == 0, result.output
    assert token not in result.stdout
    assert token not in result.stderr
    assert mock.call_args.kwargs["headers"]["Authorization"] == f"Bearer {token}", case


@pytest.mark.parametrize("case", _CONTROL_CASES)
def test_control_command_authorization_failures_are_structured(case: _ControlCase) -> None:
    response = _mock_response(
        status_code=401,
        payload={
            "error_code": "UNAUTHORIZED",
            "message": "Invalid AWF API token.",
        },
    )
    with patch("awf.cli.main.httpx.request", return_value=response) as mock:
        result = _RUNNER.invoke(app, _build_case_args(case))

    assert result.exit_code == 1, result.output
    assert "UNAUTHORIZED" in result.stderr
    mock.assert_called_once()


@pytest.mark.parametrize(
    "case,error_code,status_code",
    [
        *[
            (
                case,
                "NOT_FOUND",
                404,
            )
            for case in _CONTROL_CASES
        ],
        *[(case, case.forbidden_error_code, 409) for case in _CONTROL_CASES],
    ],
)
def test_control_command_not_found_and_state_conflicts_propagate_structured_errors(
    case: _ControlCase,
    error_code: str,
    status_code: int,
) -> None:
    response = _mock_response(
        status_code=status_code,
        payload={
            "error_code": error_code,
            "message": f"{error_code} for {case.workspace_id}",
        },
    )
    with patch("awf.cli.main.httpx.request", return_value=response) as mock:
        result = _RUNNER.invoke(app, _build_case_args(case))

    assert result.exit_code == 1, result.output
    assert error_code in result.stderr
    mock.assert_called_once()
