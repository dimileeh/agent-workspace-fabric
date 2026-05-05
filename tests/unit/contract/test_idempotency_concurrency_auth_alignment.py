"""Contract: idempotency, ``If-Match`` concurrency, and auth-failure parity.

Three sections:

* ``TestIdempotencyContract`` -- replaying the same ``Idempotency-Key`` returns
  the same operation_id on REST and on the MCP control tools that already
  expose the key. A different payload under the same key returns
  ``IDEMPOTENCY_CONFLICT`` from both surfaces.
* ``TestIfMatchContract`` -- REST/CLI propagate ``If-Match`` and surface
  ``VERSION_CONFLICT`` consistently. The parity-matrix-driven assertion pins
  the documented ``MCP partial`` state for ``If-Match``: rows whose Schema
  mentions ``VERSION_CONFLICT`` and Status is ``MCP partial`` must reference
  the ``TODO§P1-if-match-parity`` backlog slice; rows that flip to
  ``MCP implemented`` must add an ``expected_version`` parameter to the MCP
  tool. The second clause is vacuously true today and becomes load-bearing
  the moment the parity gap is closed.
* ``TestAuthFailureContract`` -- REST 401 ``UNAUTHORIZED`` propagates to the
  CLI as a non-zero exit with the canonical envelope on stderr. REST 503
  ``API_TOKEN_NOT_CONFIGURED`` works the same way. MCP tools never construct
  these codes, per the documented in-process trust boundary.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from awf.api.schemas import ErrorResponse

from tests.unit.contract.conftest import (
    ContractStack,
    call_mcp_structured,
    invoke_cli,
    seed_monitoring_workspace,
    unwrap_error_envelope,
)
from tests.unit.mcp._parity_utils import _parity_rows, _strip_backticks


@pytest.mark.unit
class TestIdempotencyContract:
    async def test_remonitor_idempotency_replay_aligned_across_rest_and_mcp(
        self,
        contract_stack: ContractStack,
    ) -> None:
        rest_workspace = await seed_monitoring_workspace(
            contract_stack.factory, title="Remon idem REST"
        )
        mcp_workspace = await seed_monitoring_workspace(
            contract_stack.factory, title="Remon idem MCP"
        )

        rest_first = await contract_stack.rest_client.post(
            f"/v1/workspaces/{rest_workspace}/remonitor",
            headers={
                **contract_stack.auth_headers,
                "Idempotency-Key": "remon-replay-rest",
            },
            json={"reason": "replay"},
        )
        rest_second = await contract_stack.rest_client.post(
            f"/v1/workspaces/{rest_workspace}/remonitor",
            headers={
                **contract_stack.auth_headers,
                "Idempotency-Key": "remon-replay-rest",
            },
            json={"reason": "replay"},
        )
        assert rest_first.status_code == 200
        assert rest_second.status_code == 200
        assert rest_first.json()["operation_id"] == rest_second.json()["operation_id"]

        mcp_first = await call_mcp_structured(
            contract_stack.mcp,
            "awf_remonitor_workspace",
            {
                "workspace_id": mcp_workspace,
                "reason": "replay",
                "idempotency_key": "remon-replay-mcp",
            },
        )
        mcp_second = await call_mcp_structured(
            contract_stack.mcp,
            "awf_remonitor_workspace",
            {
                "workspace_id": mcp_workspace,
                "reason": "replay",
                "idempotency_key": "remon-replay-mcp",
            },
        )
        assert isinstance(mcp_first, dict)
        assert isinstance(mcp_second, dict)
        assert mcp_first["operation_id"] == mcp_second["operation_id"]

    async def test_remonitor_idempotency_conflict_aligned_across_rest_and_mcp(
        self,
        contract_stack: ContractStack,
    ) -> None:
        rest_workspace = await seed_monitoring_workspace(
            contract_stack.factory, title="Remon conflict REST"
        )
        mcp_workspace = await seed_monitoring_workspace(
            contract_stack.factory, title="Remon conflict MCP"
        )

        await contract_stack.rest_client.post(
            f"/v1/workspaces/{rest_workspace}/remonitor",
            headers={
                **contract_stack.auth_headers,
                "Idempotency-Key": "remon-conflict-rest",
            },
            json={"reason": "first"},
        )
        rest_conflict = await contract_stack.rest_client.post(
            f"/v1/workspaces/{rest_workspace}/remonitor",
            headers={
                **contract_stack.auth_headers,
                "Idempotency-Key": "remon-conflict-rest",
            },
            json={"reason": "different"},
        )
        assert rest_conflict.status_code == 409
        rest_envelope = unwrap_error_envelope(rest_conflict.json())
        assert rest_envelope["error_code"] == "IDEMPOTENCY_CONFLICT"

        await call_mcp_structured(
            contract_stack.mcp,
            "awf_remonitor_workspace",
            {
                "workspace_id": mcp_workspace,
                "reason": "first",
                "idempotency_key": "remon-conflict-mcp",
            },
        )
        from tests.unit.contract.conftest import call_mcp_result

        mcp_conflict = await call_mcp_result(
            contract_stack.mcp,
            "awf_remonitor_workspace",
            {
                "workspace_id": mcp_workspace,
                "reason": "different",
                "idempotency_key": "remon-conflict-mcp",
            },
        )
        assert mcp_conflict.isError is True
        mcp_envelope = unwrap_error_envelope(mcp_conflict.structuredContent)
        assert mcp_envelope["error_code"] == "IDEMPOTENCY_CONFLICT"
        assert ErrorResponse.model_validate(mcp_envelope).error_code == ErrorResponse.model_validate(
            rest_envelope
        ).error_code


@pytest.mark.unit
class TestIfMatchContract:
    async def test_rest_if_match_stale_returns_canonical_version_conflict(
        self,
        contract_stack: ContractStack,
    ) -> None:
        workspace_id = await seed_monitoring_workspace(contract_stack.factory)

        response = await contract_stack.rest_client.post(
            f"/v1/workspaces/{workspace_id}/remonitor",
            headers={
                **contract_stack.auth_headers,
                "Idempotency-Key": "remon-stale-version",
                "If-Match": "0",
            },
            json={"reason": "stale"},
        )
        assert response.status_code == 409
        envelope = unwrap_error_envelope(response.json())
        assert envelope["error_code"] == "VERSION_CONFLICT"
        assert envelope["detail"] == {
            "expected_version": 0,
            "actual_version": 7,
        }

    def test_cli_remonitor_propagates_409_version_conflict_to_stderr(
        self,
        contract_stack: ContractStack,
    ) -> None:
        rest_envelope = ErrorResponse(
            error_code="VERSION_CONFLICT",
            message="Workspace version does not match If-Match.",
            detail={"expected_version": 1, "actual_version": 7},
        ).model_dump(mode="json")
        rest_body = {"detail": rest_envelope}

        cli_result, _request_mock = invoke_cli(
            contract_stack.cli_runner,
            [
                "workspace",
                "remonitor",
                "ws_x",
                "--idempotency-key",
                "remon-cli",
                "--if-match",
                "1",
                "--api-token",
                "secret",
            ],
            response_status=409,
            response_payload=rest_body,
        )
        assert cli_result.exit_code == 1
        stderr_payload = json.loads(cli_result.stderr or cli_result.stdout)
        assert (
            unwrap_error_envelope(stderr_payload)["error_code"] == "VERSION_CONFLICT"
        )

    def test_parity_matrix_pins_mcp_if_match_partial_state(
        self,
        contract_stack: ContractStack,
    ) -> None:
        rows = _parity_rows()
        rows_with_version_conflict = [
            row
            for row in rows
            if "VERSION_CONFLICT" in _strip_backticks(row.get("Schema / Error-Code Contract", ""))
        ]
        assert rows_with_version_conflict, (
            "Parity matrix must list at least one row with VERSION_CONFLICT in its "
            "schema column for the contract to be meaningful."
        )

        partial_rows = [r for r in rows_with_version_conflict if "partial" in r["Status"].lower()]
        for row in partial_rows:
            backlog = _strip_backticks(row.get("Backlog Slice", ""))
            assert "TODO§P1-if-match-parity" in backlog, (
                f"Row {row.get('Capability', '?')!r} marks VERSION_CONFLICT as MCP partial "
                "but does not reference TODO§P1-if-match-parity."
            )

        # Load-bearing the moment the parity gap closes: every row whose Status
        # is "MCP implemented" AND whose schema mentions VERSION_CONFLICT must
        # have an `expected_version` argument on the MCP tool. Today the
        # iteration is vacuous; tomorrow it pins the contract.
        implemented_rows = [
            r
            for r in rows_with_version_conflict
            if "MCP implemented" in r["Status"]
        ]
        for row in implemented_rows:
            tool_cell = _strip_backticks(row.get("MCP tool name", ""))
            for tool_name in [name.strip() for name in tool_cell.split(",") if name.strip()]:
                if not tool_name.startswith("awf_"):
                    continue
                tool = contract_stack.mcp._tool_manager._tools.get(tool_name)
                assert tool is not None, (
                    f"Parity matrix references missing MCP tool {tool_name!r}"
                )
                assert "expected_version" in tool.parameters["properties"], (
                    f"Tool {tool_name!r} is marked MCP implemented for VERSION_CONFLICT "
                    "but does not expose an expected_version argument; either add it "
                    "or change the parity matrix Status."
                )


@pytest.mark.unit
class TestAuthFailureContract:
    async def test_rest_unauthorized_returns_canonical_envelope(
        self,
        contract_stack: ContractStack,
    ) -> None:
        response = await contract_stack.rest_client.post(
            "/v1/workspaces/ws_irrelevant/cancel",
            json={"reason": "no auth"},
            headers={"Idempotency-Key": "cancel-no-auth"},
        )
        assert response.status_code == 401
        envelope = unwrap_error_envelope(response.json())
        assert envelope["error_code"] == "UNAUTHORIZED"
        assert response.headers.get("WWW-Authenticate") == "Bearer"

    async def test_rest_returns_503_when_api_token_not_configured(
        self,
        contract_stack: ContractStack,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from awf.common.config import get_settings

        monkeypatch.delenv("AWF_API_TOKEN", raising=False)
        get_settings.cache_clear()
        contract_stack.rest_client._transport.app.dependency_overrides.clear()  # type: ignore[attr-defined]

        try:
            response = await contract_stack.rest_client.post(
                "/v1/workspaces/ws_irrelevant/cancel",
                json={"reason": "no token"},
                headers={"Idempotency-Key": "cancel-no-token"},
            )
        finally:
            get_settings.cache_clear()

        assert response.status_code == 503
        envelope = unwrap_error_envelope(response.json())
        assert envelope["error_code"] == "API_TOKEN_NOT_CONFIGURED"

    @pytest.mark.parametrize(
        "cli_args",
        [
            [
                "workspace",
                "remonitor",
                "ws_x",
                "--idempotency-key",
                "remon",
            ],
            ["workspace", "retry", "ws_x"],
            [
                "workspace",
                "adopt-pr",
                "--repo",
                "owner/x",
                "--pr",
                "1",
            ],
            ["locks", "list"],
        ],
    )
    def test_cli_propagates_rest_401_with_nonzero_exit(
        self,
        contract_stack: ContractStack,
        cli_args: list[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("AWF_API_TOKEN", raising=False)

        rest_body: dict[str, Any] = {
            "detail": ErrorResponse(
                error_code="UNAUTHORIZED",
                message="Invalid AWF API token.",
            ).model_dump(mode="json")
        }

        cli_result, _request_mock = invoke_cli(
            contract_stack.cli_runner,
            cli_args,
            response_status=401,
            response_payload=rest_body,
        )
        assert cli_result.exit_code == 1
        text = cli_result.stderr or cli_result.stdout
        assert "UNAUTHORIZED" in text
