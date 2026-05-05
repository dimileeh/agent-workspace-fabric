"""Contract: REST canonical schemas validate the request bodies CLI and MCP build.

REST is the canonical schema source of truth for AWF. The CLI is a JSON-first
HTTP client; the MCP control tools build the same canonical request models
internally before delegating to the shared ``WorkspaceService``. These tests
prove the CLI body and the MCP-built body for each capability validate against
the same Pydantic model and normalize to the same canonical instance.
"""

from __future__ import annotations

import pytest

from awf.api.schemas import (
    PullRequestMonitorAdoptionRequest,
    WorkspaceCreateV2Request,
)

from tests.unit.contract.conftest import ContractStack, invoke_cli


@pytest.mark.unit
class TestWorkspaceCreateV2RequestContract:
    """``POST /v2/workspaces`` body must match what the CLI emits and MCP builds."""

    def test_cli_body_validates_against_canonical_schema(
        self,
        contract_stack: ContractStack,
    ) -> None:
        cli_result, request_mock = invoke_cli(
            contract_stack.cli_runner,
            [
                "workspace",
                "create",
                "--repo",
                "git@github.com:example/contract.git",
                "--title",
                "Contract create",
                "--prompt",
                "Implement the contract.",
                "--test",
                "pytest -q",
                "--test",
                "ruff check .",
            ],
            response_status=202,
            response_payload={
                "workspace_id": "ws_create",
                "status": "requested",
                "version": 1,
                "status_url": "/v1/workspaces/ws_create",
                "events_url": "/v1/workspaces/ws_create/events",
                "accepted_at": "2026-01-01T00:00:00Z",
            },
        )
        assert cli_result.exit_code == 0
        body = request_mock.call_args.kwargs["json"]

        validated = WorkspaceCreateV2Request.model_validate(body)
        assert validated.repo.url == "git@github.com:example/contract.git"
        assert validated.task.title == "Contract create"
        assert validated.validation.commands == ["pytest -q", "ruff check ."]

    async def test_mcp_built_body_validates_against_canonical_schema(
        self,
        contract_stack: ContractStack,
    ) -> None:
        captured: dict[str, WorkspaceCreateV2Request] = {}

        class _CreatedStub:
            def model_dump(self, *, mode: str = "json") -> dict[str, str]:
                return {"workspace_id": "ws_create_mcp"}

        async def spy_create_v2(req: WorkspaceCreateV2Request) -> _CreatedStub:
            captured["req"] = req
            return _CreatedStub()

        from awf.mcp.server import WorkspaceService, build_mcp_server

        service = WorkspaceService(contract_stack.factory, settings=contract_stack.settings)
        service.create_v2 = spy_create_v2  # type: ignore[method-assign]
        mcp = build_mcp_server(service=service)

        from tests.unit.contract.conftest import call_mcp_structured

        await call_mcp_structured(
            mcp,
            "awf_create_workspace_v2",
            {
                "repo_url": "git@github.com:example/contract.git",
                "task_title": "Contract create",
                "task_prompt": "Implement the contract.",
                "validation_commands": ["pytest -q", "ruff check ."],
            },
        )
        assert "req" in captured
        body = captured["req"].model_dump(mode="json")
        validated = WorkspaceCreateV2Request.model_validate(body)
        assert validated.repo.url == "git@github.com:example/contract.git"
        assert validated.task.title == "Contract create"
        assert validated.validation.commands == ["pytest -q", "ruff check ."]


@pytest.mark.unit
class TestAdoptPullRequestMonitorRequestContract:
    """Existing PR adoption body shape must agree across REST, CLI, and MCP."""

    @pytest.mark.parametrize(
        ("cli_args", "expected_repo_slug", "expected_repo_url", "expected_pr_url"),
        [
            (
                ["--repo", "dimileeh/aira-web", "--pr", "277"],
                "dimileeh/aira-web",
                None,
                None,
            ),
            (
                [
                    "--repo",
                    "https://github.com/dimileeh/aira-web",
                    "--pr",
                    "277",
                ],
                None,
                "https://github.com/dimileeh/aira-web",
                None,
            ),
            (
                [
                    "--pr-url",
                    "https://github.com/dimileeh/aira-web/pull/277",
                ],
                None,
                None,
                "https://github.com/dimileeh/aira-web/pull/277",
            ),
        ],
    )
    def test_cli_body_normalizes_repo_input_into_canonical_schema(
        self,
        contract_stack: ContractStack,
        cli_args: list[str],
        expected_repo_slug: str | None,
        expected_repo_url: str | None,
        expected_pr_url: str | None,
    ) -> None:
        cli_result, request_mock = invoke_cli(
            contract_stack.cli_runner,
            ["workspace", "adopt-pr", *cli_args],
            response_status=202,
            response_payload={
                "workspace_id": "ws_adopt",
                "status": "requested",
                "version": 1,
                "task_id": None,
                "attempt_id": None,
                "candidate_id": None,
                "repo_slug": "dimileeh/aira-web",
                "repo_url": "https://github.com/dimileeh/aira-web",
                "pr_number": 277,
                "pr_url": "https://github.com/dimileeh/aira-web/pull/277",
                "head_ref": "feature/x",
                "base_ref": "development",
                "auto_merge": True,
                "monitor_policy": {},
                "attached_existing": False,
                "validation_provenance": {"freshness_status": "unavailable"},
                "status_url": "/v1/workspaces/ws_adopt",
                "events_url": "/v1/workspaces/ws_adopt/events",
                "logs_url": "/v1/workspaces/ws_adopt/logs",
            },
        )
        assert cli_result.exit_code == 0
        body = request_mock.call_args.kwargs["json"]

        validated = PullRequestMonitorAdoptionRequest.model_validate(body)
        assert validated.repo_slug == expected_repo_slug
        assert validated.repo_url == expected_repo_url
        assert validated.pr_url == expected_pr_url

    async def test_mcp_built_body_validates_against_canonical_schema(
        self,
        contract_stack: ContractStack,
    ) -> None:
        captured: dict[str, PullRequestMonitorAdoptionRequest] = {}

        async def spy_adopt(req: PullRequestMonitorAdoptionRequest):
            captured["req"] = req
            from awf.api.schemas import PullRequestMonitorAdoptionResponse

            return PullRequestMonitorAdoptionResponse(
                workspace_id="ws_adopt",
                status="requested",
                version=1,
                repo_slug="dimileeh/aira-web",
                repo_url="https://github.com/dimileeh/aira-web",
                pr_number=277,
                pr_url="https://github.com/dimileeh/aira-web/pull/277",
                head_ref="feature/x",
                base_ref="development",
                auto_merge=True,
                attached_existing=False,
                status_url="/v1/workspaces/ws_adopt",
                events_url="/v1/workspaces/ws_adopt/events",
                logs_url="/v1/workspaces/ws_adopt/logs",
            )

        from awf.mcp.server import WorkspaceService, build_mcp_server

        service = WorkspaceService(contract_stack.factory, settings=contract_stack.settings)
        service.adopt_pull_request_monitor = spy_adopt  # type: ignore[method-assign]
        mcp = build_mcp_server(service=service)

        from tests.unit.contract.conftest import call_mcp_structured

        await call_mcp_structured(
            mcp,
            "awf_adopt_pull_request_monitor",
            {"repo_slug": "dimileeh/aira-web", "pr_number": 277},
        )
        assert captured["req"].repo_slug == "dimileeh/aira-web"
        assert captured["req"].pr_number == 277
        assert captured["req"].pr_url is None

        validated = PullRequestMonitorAdoptionRequest.model_validate(
            captured["req"].model_dump(mode="json")
        )
        assert validated == captured["req"]


@pytest.mark.unit
class TestRemonitorRequestContract:
    """Remonitor must surface ``Idempotency-Key`` and ``If-Match`` consistently."""

    def test_cli_forwards_idempotency_key_and_if_match_headers(
        self,
        contract_stack: ContractStack,
    ) -> None:
        cli_result, request_mock = invoke_cli(
            contract_stack.cli_runner,
            [
                "workspace",
                "remonitor",
                "ws_remon",
                "--idempotency-key",
                "remon-key-1",
                "--if-match",
                "7",
                "--api-token",
                "secret",
                "--reason",
                "operator recovery",
            ],
            response_status=200,
            response_payload={
                "workspace_id": "ws_remon",
                "operation_id": "op_remon",
                "operation_status": "succeeded",
                "status": "monitoring_pr",
                "message": "ok",
            },
        )
        assert cli_result.exit_code == 0
        kwargs = request_mock.call_args.kwargs
        assert kwargs["headers"]["Idempotency-Key"] == "remon-key-1"
        assert kwargs["headers"]["If-Match"] == "7"
        assert kwargs["headers"]["Authorization"] == "Bearer secret"
        assert kwargs["json"] == {"reason": "operator recovery"}

    def test_mcp_remonitor_tool_arguments_pin_partial_if_match_state(
        self,
        contract_stack: ContractStack,
    ) -> None:
        """Document the current ``MCP partial`` state for the parity matrix.

        ``awf_remonitor_workspace`` accepts ``idempotency_key`` but does not
        yet accept ``expected_version`` -- this is owned by the
        ``TODO§P1-if-match-parity`` slice. Pin both facts so a future change
        that flips the matrix row to ``MCP implemented`` without adding the
        arg fails this contract test.
        """

        tool = contract_stack.mcp._tool_manager._tools["awf_remonitor_workspace"]
        properties = tool.parameters["properties"]
        assert "idempotency_key" in properties
        assert "expected_version" not in properties
