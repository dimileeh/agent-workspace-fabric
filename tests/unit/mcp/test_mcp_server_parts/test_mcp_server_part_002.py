"""MCP server + tool behaviour tests.

We exercise the tools via ``mcp.call_tool(name, args)`` (FastMCP's in-process
harness) against a throwaway PostgreSQL. This validates:
- All tools are registered under the expected names.
- Each tool's happy path returns the same payload shape as the REST API.
- wait_for_workspace exits on terminal state without hanging.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from mcp.types import CallToolResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.schemas import (
    OperationResponse,
    WorkspaceControlResponse,
)
from awf.common.config import Settings
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import (
    SecretLeaseIssue,
    SecretLeaseRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.mcp.server import WorkspaceService, build_mcp_server
from awf.service.controls import WorkspaceControlError
from awf.service.disk import DiskCheck
from tests.postgres import postgres_test_engine
from tests.unit.helpers import assert_no_internal_error_fields

_PROVIDER_AUTH_ENV_KEYS = (
    "OPENAI_API_KEY",
    "OPENAI_API_TOKEN",
    "CODEX_API_KEY",
    "CODEX_AUTH_TOKEN",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@pytest.fixture
def mcp(factory: async_sessionmaker[AsyncSession]):  # type: ignore[no-untyped-def]
    service = WorkspaceService(factory)
    return build_mcp_server(service=service)


_CREATE_ARGS: dict[str, object] = {
    "repo_url": "git@github.com:dimileeh/aira-agent.git",
    "base_branch": "development",
    "task_title": "Add docstring",
    "task_prompt": "Add a one-line docstring to src/module/__init__.py.",
    "agent": "codex",
    "validation_commands": ["pytest -q"],
    "provider_readiness_override": True,
    "provider_readiness_override_reason": "mcp default create fixture",
}


def _operation_response() -> OperationResponse:
    return OperationResponse(
        id="op_prevalidated",
        workspace_id="ws_prevalidated",
        type="validate",
        status="succeeded",
        error_code=None,
        error_message=None,
        payload=None,
        result=None,
        idempotency_key=None,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        started_at=None,
        finished_at=None,
    )


def _low_disk_check(settings: Settings) -> DiskCheck:
    return DiskCheck(
        path=settings.work_dir,
        checked_path=settings.work_dir,
        total_bytes=100,
        used_bytes=95,
        free_bytes=5,
        percent_free=5.0,
        threshold_bytes=10,
        ok=False,
        status="fail",
        reason="INSUFFICIENT_DISK",
        detail="free_bytes=5 threshold_bytes=10",
    )


def _ok_disk_check(settings: Settings) -> DiskCheck:
    return DiskCheck(
        path=settings.work_dir,
        checked_path=settings.work_dir,
        total_bytes=100,
        used_bytes=20,
        free_bytes=80,
        percent_free=80.0,
        threshold_bytes=10,
        ok=True,
        status="ok",
        reason="SUFFICIENT_DISK",
        detail=None,
    )


async def _call(mcp, name, args) -> object:  # type: ignore[no-untyped-def]
    """Unwrap FastMCP's call_tool payload.

    FastMCP returns ``(content, structured)`` where ``structured`` is the
    tool's return value for dict returns, or ``{"result": <value>}`` for
    primitive / None / list returns. This helper normalises to the underlying
    value so tests can assert against it directly.
    """
    result = await mcp.call_tool(name, args)
    if isinstance(result, CallToolResult):
        return result.structuredContent
    _, payload = result
    if isinstance(payload, dict) and list(payload.keys()) == ["result"]:
        return payload["result"]
    return payload


def _workspace_id(payload: object) -> str:
    assert isinstance(payload, dict)
    return str(payload["workspace_id"])


def _optional_string_schema(schema: dict[str, object]) -> dict[str, object]:
    any_of = schema.get("anyOf")
    assert isinstance(any_of, list)
    string_schema = next(
        (item for item in any_of if isinstance(item, dict) and item.get("type") == "string"),
        None,
    )
    assert string_schema is not None, f"Could not find string schema in anyOf: {any_of}"
    assert isinstance(string_schema, dict)
    return string_schema


def _optional_object_schema(schema: dict[str, object]) -> dict[str, object]:
    any_of = schema.get("anyOf")
    if any_of is None:
        assert schema.get("type") == "object"
        return schema

    assert isinstance(any_of, list)
    object_schema = next(
        (item for item in any_of if isinstance(item, dict) and item.get("type") == "object"),
        None,
    )
    assert object_schema is not None, f"Could not find object schema in anyOf: {any_of}"
    assert isinstance(object_schema, dict)
    return object_schema


def _assert_idempotency_key_schema(schema: dict[str, object]) -> None:
    string_schema = _optional_string_schema(schema)
    assert str(schema["description"]).startswith("Required idempotency key")
    assert schema["minLength"] == 1
    assert string_schema["maxLength"] == 128
    assert "default" not in schema


class _RecordingControlService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def cancel_workspace(
        self,
        workspace_id: str,
        *,
        reason: str | None,
        stop_stack: bool,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> WorkspaceControlResponse:
        self.calls.append(
            (
                "cancel",
                {
                    "workspace_id": workspace_id,
                    "reason": reason,
                    "stop_stack": stop_stack,
                    "idempotency_key": idempotency_key,
                    "expected_version": expected_version,
                },
            )
        )
        return WorkspaceControlResponse(
            workspace_id=workspace_id,
            operation_id="op_cancel",
            operation_status="succeeded",
            status="cancelled",
            message="workspace cancellation requested",
        )

    async def stop_workspace(
        self,
        workspace_id: str,
        *,
        reason: str | None,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> WorkspaceControlResponse:
        self.calls.append(
            (
                "stop",
                {
                    "workspace_id": workspace_id,
                    "reason": reason,
                    "idempotency_key": idempotency_key,
                    "expected_version": expected_version,
                },
            )
        )
        return WorkspaceControlResponse(
            workspace_id=workspace_id,
            operation_id="op_stop",
            operation_status="succeeded",
            status="cancelled",
            message="workspace stack stopped",
        )

    async def destroy_workspace(
        self,
        workspace_id: str,
        *,
        force: bool,
        remove_volumes: bool,
        remove_worktree: bool,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> WorkspaceControlResponse:
        self.calls.append(
            (
                "destroy",
                {
                    "workspace_id": workspace_id,
                    "force": force,
                    "remove_volumes": remove_volumes,
                    "remove_worktree": remove_worktree,
                    "idempotency_key": idempotency_key,
                    "expected_version": expected_version,
                },
            )
        )
        return WorkspaceControlResponse(
            workspace_id=workspace_id,
            operation_id="op_destroy",
            operation_status="succeeded",
            status="destroyed",
            message="workspace destroyed",
        )


class _FailingControlService(_RecordingControlService):
    async def cancel_workspace(
        self,
        workspace_id: str,
        *,
        reason: str | None,
        stop_stack: bool,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> WorkspaceControlResponse:
        del workspace_id, reason, stop_stack, idempotency_key, expected_version
        raise WorkspaceControlError(error_code="NOPE", message="cancel refused")

    async def stop_workspace(
        self,
        workspace_id: str,
        *,
        reason: str | None,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> WorkspaceControlResponse:
        del workspace_id, reason, idempotency_key, expected_version
        raise WorkspaceControlError(error_code="NOPE", message="stop refused")


class TestCreateWorkspace:
    @pytest.fixture(autouse=True)
    def _clear_provider_auth_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for key in _PROVIDER_AUTH_ENV_KEYS:
            monkeypatch.delenv(key, raising=False)

    @pytest.mark.unit
    async def test_persists_canonical_create_contract_fields(
        self,
        mcp,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:  # type: ignore[no-untyped-def]
        payload = await _call(
            mcp,
            "awf_create_workspace",
            {
                "repo_url": "git@github.com:example/app.git",
                "base_branch": "main",
                "task_title": "Add planner hook",
                "task_prompt": "Implement the planner hook.",
                "task_kind": "feature_branch_pr",
                "agent": "claude_code",
                "model": "claude-opus-4-7",
                "effort": "xhigh",
                "task_external_id": "AIRA-42",
                "profile_ref": "python",
                "profile": {
                    "name": "inline-python",
                    "validation": {"requested_tier": 2},
                    "monitor": {"initial_review_grace_period_seconds": 333},
                },
                "validation_commands": ["uv run pytest tests/unit -q"],
                "requested_tier": 2,
                "auto_merge": False,
                "initial_review_grace_period_seconds": 12.5,
                "out_of_scope_changes": {
                    "mode": "block",
                    "allowlist_patterns": ["src/**", "docs/**"],
                },
                "provider_recovery": {
                    "max_fallback_attempts": 1,
                    "fallbacks": [
                        {"agent": "codex", "provider": "openai", "model": "gpt-5.5"},
                    ],
                },
                "provider_readiness_override": True,
                "provider_readiness_override_reason": "mcp test override",
            },
        )

        assert isinstance(payload, dict)
        ws_id = payload["workspace_id"]
        assert payload["status_url"] == f"/v1/workspaces/{ws_id}"
        assert payload["events_url"] == f"/v1/workspaces/{ws_id}/events"
        assert "accepted_at" in payload
        assert "id" not in payload
        assert "task_class" not in payload
        async with factory() as session:
            ws = await WorkspaceRepository(session).get(str(ws_id))

        assert ws is not None
        assert ws.repo_url == "git@github.com:example/app.git"
        assert ws.branch_base == "main"
        assert ws.task_title == "Add planner hook"
        assert ws.task_prompt == "Implement the planner hook."
        assert ws.task_external_id == "AIRA-42"
        assert ws.task_kind == "feature_branch_pr"
        assert ws.agent == "claude_code"
        assert ws.task_policy["agent_model"] == "claude-opus-4-7"
        assert ws.task_policy["agent_effort"] == "xhigh"
        assert ws.task_policy["provider_readiness_preflight"]["provider"] == "claude_code"
        assert ws.profile_ref == "python"
        assert ws.requested_profile is not None
        assert ws.requested_profile["name"] == "inline-python"
        assert ws.resolved_profile is not None
        assert ws.resolved_profile["validation"]["requested_tier"] == 2
        assert [item["command"] for item in ws.resolved_profile["phases"]["validate"]] == [
            "uv run pytest tests/unit -q"
        ]
        assert ws.test_commands == ["uv run pytest tests/unit -q"]
        assert ws.auto_merge is False
        assert ws.initial_review_grace_period_seconds == 12.5
        assert ws.task_policy["out_of_scope_changes"] == {
            "mode": "block",
            "allowlist_patterns": ["src/**", "docs/**"],
        }
        assert ws.task_policy["provider_recovery"] == {
            "max_fallback_attempts": 1,
            "fallbacks": [
                {"agent": "codex", "provider": "openai", "model": "gpt-5.5"},
            ],
        }

    @pytest.mark.unit
    async def test_create_workspace_accepts_legacy_flat_arguments(
        self,
        mcp,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:  # type: ignore[no-untyped-def]
        payload = await _call(
            mcp,
            "awf_create_workspace",
            {
                "repo_url": "git@github.com:example/legacy.git",
                "branch_base": "legacy-base",
                "task_title": "Legacy MCP create",
                "task_prompt": "Preserve older MCP create arguments.",
                "test_commands": ["uv run pytest tests/unit/mcp -q"],
                "requires_database": True,
                "provider_readiness_override": True,
                "provider_readiness_override_reason": "mcp legacy create compatibility",
            },
        )

        assert isinstance(payload, dict)
        async with factory() as session:
            ws = await WorkspaceRepository(session).get(str(payload["workspace_id"]))

        assert ws is not None
        assert ws.branch_base == "legacy-base"
        assert ws.profile_ref == "aira"
        assert ws.requires_database is True
        assert ws.test_commands == ["uv run pytest tests/unit/mcp -q"]

    @pytest.mark.unit
    async def test_create_workspace_accepts_companion_compose_up_timeout(
        self,
        mcp,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:  # type: ignore[no-untyped-def]
        payload = await _call(
            mcp,
            "awf_create_workspace",
            {
                **_CREATE_ARGS,
                "task_title": "MCP companion timeout",
                "companions": [
                    {
                        "name": "backend",
                        "repo_url": "git@github.com:example/backend.git",
                        "compose_up_timeout_seconds": 900,
                    }
                ],
            },
        )

        assert isinstance(payload, dict)
        async with factory() as session:
            ws = await WorkspaceRepository(session).get(str(payload["workspace_id"]))

        assert ws is not None
        assert ws.task_policy["companions"][0]["compose_up_timeout_seconds"] == 900

    @pytest.mark.unit
    async def test_create_workspace_accepts_legacy_env_profile_alias(
        self,
        mcp,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:  # type: ignore[no-untyped-def]
        payload = await _call(
            mcp,
            "awf_create_workspace",
            {
                "repo_url": "git@github.com:example/legacy-profile.git",
                "branch_base": "legacy-base",
                "task_title": "Legacy MCP profile alias",
                "task_prompt": "Preserve older MCP env_profile create argument.",
                "env_profile": "python",
                "test_commands": ["uv run pytest tests/unit/mcp -q"],
                "provider_readiness_override": True,
                "provider_readiness_override_reason": "mcp env_profile compatibility",
            },
        )

        assert isinstance(payload, dict)
        async with factory() as session:
            ws = await WorkspaceRepository(session).get(str(payload["workspace_id"]))

        assert ws is not None
        assert ws.branch_base == "legacy-base"
        assert ws.profile_ref == "python"
        assert ws.test_commands == ["uv run pytest tests/unit/mcp -q"]

    @pytest.mark.unit
    async def test_create_workspace_omitted_branch_preserves_legacy_development_default(
        self,
        mcp,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:  # type: ignore[no-untyped-def]
        payload = await _call(
            mcp,
            "awf_create_workspace",
            {
                "repo_url": "git@github.com:example/legacy-default.git",
                "task_title": "Legacy MCP branch default",
                "task_prompt": "Preserve older MCP create branch fallback.",
                "provider_readiness_override": True,
                "provider_readiness_override_reason": "mcp legacy branch default regression",
            },
        )

        assert isinstance(payload, dict)
        async with factory() as session:
            ws = await WorkspaceRepository(session).get(str(payload["workspace_id"]))

        assert ws is not None
        assert ws.branch_base == "development"

    @pytest.mark.unit
    async def test_create_workspace_sync_release_pr_omitted_base_defaults_to_main(
        self,
        mcp,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:  # type: ignore[no-untyped-def]
        payload = await _call(
            mcp,
            "awf_create_workspace",
            {
                "repo_url": "git@github.com:example/release-sync-default.git",
                "task_kind": "sync_release_pr",
                "task_title": "Release sync default target",
                "task_prompt": "Open the standing release PR for the default target.",
                "provider_readiness_override": True,
                "provider_readiness_override_reason": "mcp release-sync default target regression",
            },
        )

        assert isinstance(payload, dict)
        async with factory() as session:
            ws = await WorkspaceRepository(session).get(str(payload["workspace_id"]))

        assert ws is not None
        # Omitting base_branch for sync_release_pr must target main (development ->
        # main), not the legacy development default that degenerates to
        # development -> development and exits NO_CHANGES_TO_SYNC.
        assert ws.branch_base == "main"
        assert ws.task_policy["release_sync"] == {
            "source_branch": "development",
            "target_branch": "main",
        }

    @pytest.mark.unit
    async def test_create_workspace_sync_release_pr_honors_explicit_base(
        self,
        mcp,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:  # type: ignore[no-untyped-def]
        payload = await _call(
            mcp,
            "awf_create_workspace",
            {
                "repo_url": "git@github.com:example/release-sync-explicit.git",
                "base_branch": "release/2026.05",
                "task_kind": "sync_release_pr",
                "task_title": "Release sync explicit target",
                "task_prompt": "Open the release PR against an explicit target branch.",
                "provider_readiness_override": True,
                "provider_readiness_override_reason": "mcp release-sync explicit target regression",
            },
        )

        assert isinstance(payload, dict)
        async with factory() as session:
            ws = await WorkspaceRepository(session).get(str(payload["workspace_id"]))

        assert ws is not None
        assert ws.branch_base == "release/2026.05"
        assert ws.task_policy["release_sync"]["target_branch"] == "release/2026.05"

    @pytest.mark.unit
    async def test_create_workspace_sync_release_pr_honors_explicit_source_branch(
        self,
        mcp,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:  # type: ignore[no-untyped-def]
        payload = await _call(
            mcp,
            "awf_create_workspace",
            {
                "repo_url": "git@github.com:example/release-sync-source.git",
                "base_branch": "main",
                "source_branch": "release/staging",
                "task_kind": "sync_release_pr",
                "task_title": "Release sync explicit source",
                "task_prompt": "Open the release PR from a non-default source branch.",
                "provider_readiness_override": True,
                "provider_readiness_override_reason": "mcp release-sync source override regression",
            },
        )

        assert isinstance(payload, dict)
        async with factory() as session:
            ws = await WorkspaceRepository(session).get(str(payload["workspace_id"]))

        assert ws is not None
        # The MCP source_branch override must thread through to the release-sync
        # policy so callers can sync a non-default branch, matching CLI/REST.
        assert ws.task_policy["release_sync"] == {
            "source_branch": "release/staging",
            "target_branch": "main",
        }

    @pytest.mark.unit
    async def test_create_workspace_accepts_matching_legacy_and_canonical_aliases(
        self,
        mcp,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:  # type: ignore[no-untyped-def]
        payload = await _call(
            mcp,
            "awf_create_workspace",
            {
                "repo_url": "git@github.com:example/matched-aliases.git",
                "base_branch": "main",
                "branch_base": "main",
                "task_title": "Matched MCP create aliases",
                "task_prompt": "Accept callers that send both alias forms with matching values.",
                "validation_commands": ["uv run pytest tests/unit/mcp -q"],
                "test_commands": ["uv run pytest tests/unit/mcp -q"],
                "provider_readiness_override": True,
                "provider_readiness_override_reason": "mcp matched alias compatibility",
            },
        )

        assert isinstance(payload, dict)
        async with factory() as session:
            ws = await WorkspaceRepository(session).get(str(payload["workspace_id"]))

        assert ws is not None
        assert ws.branch_base == "main"
        assert ws.test_commands == ["uv run pytest tests/unit/mcp -q"]

    @pytest.mark.unit
    async def test_create_workspace_rejects_conflicting_branch_aliases(
        self,
        mcp,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:  # type: ignore[no-untyped-def]
        result = await mcp.call_tool(
            "awf_create_workspace",
            {
                "repo_url": "git@github.com:example/conflicting-branch.git",
                "base_branch": "main",
                "branch_base": "release/next",
                "task_title": "Conflicting branch aliases",
                "task_prompt": "Reject mismatched base branch alias values.",
                "provider_readiness_override": True,
                "provider_readiness_override_reason": "mcp branch alias conflict regression",
            },
        )

        assert isinstance(result, CallToolResult)
        assert result.isError is True
        assert result.structuredContent == {
            "error_code": "INVALID_REQUEST",
            "message": "Provide either base_branch or branch_base, or ensure they match.",
            "detail": None,
        }
        assert_no_internal_error_fields(result.structuredContent)
        async with factory() as session:
            rows = await WorkspaceRepository(session).list(limit=10)
        assert rows == []

    @pytest.mark.unit
    async def test_create_workspace_rejects_conflicting_profile_aliases(
        self,
        mcp,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:  # type: ignore[no-untyped-def]
        result = await mcp.call_tool(
            "awf_create_workspace",
            {
                "repo_url": "git@github.com:example/conflicting-profile.git",
                "base_branch": "main",
                "task_title": "Conflicting profile aliases",
                "task_prompt": "Reject mismatched profile alias values.",
                "profile_ref": "node",
                "env_profile": "python",
                "provider_readiness_override": True,
                "provider_readiness_override_reason": "mcp profile alias conflict regression",
            },
        )

        assert isinstance(result, CallToolResult)
        assert result.isError is True
        assert result.structuredContent == {
            "error_code": "INVALID_REQUEST",
            "message": "Provide either profile_ref or env_profile, or ensure they match.",
            "detail": None,
        }
        assert_no_internal_error_fields(result.structuredContent)
        async with factory() as session:
            rows = await WorkspaceRepository(session).list(limit=10)
        assert rows == []

    @pytest.mark.unit
    async def test_create_workspace_rejects_database_shortcut_conflicting_profile_ref(
        self,
        mcp,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:  # type: ignore[no-untyped-def]
        result = await mcp.call_tool(
            "awf_create_workspace",
            {
                "repo_url": "git@github.com:example/conflicting-database-profile.git",
                "base_branch": "main",
                "task_title": "Conflicting database profile shortcut",
                "task_prompt": "Reject requires_database when profile_ref is explicit.",
                "profile_ref": "python",
                "requires_database": True,
                "provider_readiness_override": True,
                "provider_readiness_override_reason": "mcp database profile conflict regression",
            },
        )

        assert isinstance(result, CallToolResult)
        assert result.isError is True
        assert result.structuredContent == {
            "error_code": "INVALID_REQUEST",
            "message": "Provide either requires_database or profile_ref/env_profile='aira'.",
            "detail": None,
        }
        assert_no_internal_error_fields(result.structuredContent)
        async with factory() as session:
            rows = await WorkspaceRepository(session).list(limit=10)
        assert rows == []

    @pytest.mark.unit
    async def test_create_workspace_rejects_database_shortcut_conflicting_env_profile(
        self,
        mcp,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:  # type: ignore[no-untyped-def]
        result = await mcp.call_tool(
            "awf_create_workspace",
            {
                "repo_url": "git@github.com:example/conflicting-database-env-profile.git",
                "base_branch": "main",
                "task_title": "Conflicting database env profile shortcut",
                "task_prompt": "Reject requires_database when env_profile is explicit.",
                "env_profile": "python",
                "requires_database": True,
                "provider_readiness_override": True,
                "provider_readiness_override_reason": "mcp database env profile conflict regression",
            },
        )

        assert isinstance(result, CallToolResult)
        assert result.isError is True
        assert result.structuredContent == {
            "error_code": "INVALID_REQUEST",
            "message": "Provide either requires_database or profile_ref/env_profile='aira'.",
            "detail": None,
        }
        assert_no_internal_error_fields(result.structuredContent)
        async with factory() as session:
            rows = await WorkspaceRepository(session).list(limit=10)
        assert rows == []

    @pytest.mark.unit
    async def test_create_workspace_rejects_conflicting_validation_command_aliases(
        self,
        mcp,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:  # type: ignore[no-untyped-def]
        result = await mcp.call_tool(
            "awf_create_workspace",
            {
                "repo_url": "git@github.com:example/conflicting-validation.git",
                "base_branch": "main",
                "task_title": "Conflicting validation aliases",
                "task_prompt": "Reject mismatched validation command alias values.",
                "validation_commands": ["uv run pytest tests/unit -q"],
                "test_commands": ["uv run pytest tests/unit/mcp -q"],
                "provider_readiness_override": True,
                "provider_readiness_override_reason": "mcp validation alias conflict regression",
            },
        )

        assert isinstance(result, CallToolResult)
        assert result.isError is True
        assert result.structuredContent == {
            "error_code": "INVALID_REQUEST",
            "message": (
                "Provide either validation_commands or test_commands, or ensure they match."
            ),
            "detail": None,
        }
        assert_no_internal_error_fields(result.structuredContent)
        async with factory() as session:
            rows = await WorkspaceRepository(session).list(limit=10)
        assert rows == []

    @pytest.mark.unit
    async def test_policy_metadata_round_trips_through_create_get_and_list(
        self,
        mcp,
    ) -> None:  # type: ignore[no-untyped-def]
        created = await _call(
            mcp,
            "awf_create_workspace",
            {
                "repo_url": "git@github.com:example/docs.git",
                "base_branch": "main",
                "task_title": "Document policy metadata",
                "task_prompt": "Update the docs.",
                "task_kind": "feature_branch_pr",
                "task_class": "docs_task",
                "owned_paths": ["README.md", "docs/**"],
                "provider_readiness_override": True,
                "provider_readiness_override_reason": "mcp metadata test fixture",
            },
        )

        assert isinstance(created, dict)
        ws_id = created["workspace_id"]
        fetched = await _call(mcp, "awf_get_workspace", {"workspace_id": ws_id})
        listed = await _call(mcp, "awf_list_workspaces", {"limit": 10})

        assert fetched is not None
        assert fetched["task_class"] == "docs_task"  # type: ignore[index]
        assert fetched["owned_paths"] == ["README.md", "docs/**"]  # type: ignore[index]
        assert isinstance(listed, list)
        assert listed[0]["task_class"] == "docs_task"
        assert listed[0]["owned_paths"] == ["README.md", "docs/**"]

    @pytest.mark.unit
    async def test_create_workspace_returns_structured_provider_preflight_error(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        service = WorkspaceService(
            factory,
            settings=Settings(
                _env_file=None,
                host_home=str(tmp_path / "home"),
                docker_host="",
            ),
        )
        mcp = build_mcp_server(service=service)

        result = await mcp.call_tool(
            "awf_create_workspace",
            {
                "repo_url": "git@github.com:example/docs.git",
                "base_branch": "main",
                "task_title": "Document provider preflight",
                "task_prompt": "Update the docs.",
            },
        )

        assert isinstance(result, CallToolResult)
        assert result.isError is True
        assert result.structuredContent is not None
        assert result.structuredContent["error_code"] == "PROVIDER_READINESS_PRECHECK_FAILED"
        preflight = result.structuredContent["detail"]["provider_readiness_preflight"]
        assert preflight["provider"] == "codex"
        assert preflight["model"] == "gpt-5.5"
        assert preflight["blocks_launch"] is True

    @pytest.mark.unit
    async def test_create_workspace_rejects_insufficient_disk_without_creating_row(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        settings = Settings(
            _env_file=None,
            work_dir=str(tmp_path / "awf-state"),
        )
        mcp = build_mcp_server(
            service=WorkspaceService(factory, settings=settings),
            settings=settings,
            disk_check_provider=_low_disk_check,
        )

        result = await mcp.call_tool(
            "awf_create_workspace",
            {
                "repo_url": "git@github.com:example/docs.git",
                "base_branch": "main",
                "task_title": "Document low disk admission",
                "task_prompt": "Update the docs.",
                "provider_readiness_override": True,
                "provider_readiness_override_reason": "mcp disk admission test fixture",
            },
        )

        assert isinstance(result, CallToolResult)
        assert result.isError is True
        assert result.structuredContent is not None
        assert result.structuredContent["error_code"] == "INSUFFICIENT_DISK"
        assert result.structuredContent["detail"]["disk"]["reason"] == "INSUFFICIENT_DISK"
        async with factory() as session:
            rows = await WorkspaceRepository(session).list(limit=10)
        assert rows == []

    @pytest.mark.unit
    async def test_create_workspace_idempotency_key_still_checks_disk_for_new_workspace(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        settings = Settings(
            _env_file=None,
            work_dir=str(tmp_path / "awf-state"),
        )
        mcp = build_mcp_server(
            service=WorkspaceService(factory, settings=settings),
            settings=settings,
            disk_check_provider=_low_disk_check,
        )

        result = await mcp.call_tool(
            "awf_create_workspace",
            {
                "repo_url": "git@github.com:example/docs.git",
                "base_branch": "main",
                "task_title": "Document low disk idempotent admission",
                "task_prompt": "Update the docs.",
                "idempotency_key": "mcp-create-v2-low-disk",
                "provider_readiness_override": True,
                "provider_readiness_override_reason": "mcp disk admission test fixture",
            },
        )

        assert isinstance(result, CallToolResult)
        assert result.isError is True
        assert result.structuredContent is not None
        assert result.structuredContent["error_code"] == "INSUFFICIENT_DISK"
        assert result.structuredContent["detail"]["disk"]["reason"] == "INSUFFICIENT_DISK"
        async with factory() as session:
            rows = await WorkspaceRepository(session).list(limit=10)
        assert rows == []

    @pytest.mark.unit
    async def test_create_workspace_override_returns_preflight_summary(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        service = WorkspaceService(
            factory,
            settings=Settings(
                _env_file=None,
                host_home=str(tmp_path / "home"),
                docker_host="",
            ),
        )
        mcp = build_mcp_server(service=service)

        payload = await _call(
            mcp,
            "awf_create_workspace",
            {
                "repo_url": "git@github.com:example/docs.git",
                "base_branch": "main",
                "task_title": "Document provider preflight override",
                "task_prompt": "Update the docs.",
                "provider_readiness_override": True,
                "provider_readiness_override_reason": "operator verified local auth",
            },
        )

        assert isinstance(payload, dict)
        preflight = payload["provider_readiness_preflight"]
        assert preflight["provider"] == "codex"
        assert preflight["override_used"] is True
        assert preflight["override_reason"] == "operator verified local auth"

    @pytest.mark.unit
    async def test_create_workspace_idempotency_key_replays_or_conflicts(
        self,
        mcp,
    ) -> None:  # type: ignore[no-untyped-def]
        args = {
            "repo_url": "git@github.com:example/docs.git",
            "base_branch": "main",
            "task_title": "Document MCP idempotency",
            "task_prompt": "Update the docs.",
            "idempotency_key": "mcp-create-v2-replay",
            "provider_readiness_override": True,
            "provider_readiness_override_reason": "mcp idempotency test fixture",
        }

        first = await _call(mcp, "awf_create_workspace", args)
        replay = await _call(mcp, "awf_create_workspace", args)
        conflict = await mcp.call_tool(
            "awf_create_workspace",
            {**args, "task_title": "Changed MCP idempotency title"},
        )

        assert isinstance(first, dict)
        assert isinstance(replay, dict)
        assert replay["workspace_id"] == first["workspace_id"]
        assert isinstance(conflict, CallToolResult)
        assert conflict.isError is True
        assert conflict.structuredContent is not None
        assert conflict.structuredContent["error_code"] == "IDEMPOTENCY_CONFLICT"

    @pytest.mark.unit
    async def test_create_workspace_idempotency_replay_skips_disk_check(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        settings = Settings(
            _env_file=None,
            work_dir=str(tmp_path / "awf-state"),
        )
        calls = 0

        def counted_disk_check(settings: Settings) -> DiskCheck:
            nonlocal calls
            calls += 1
            return _ok_disk_check(settings)

        mcp = build_mcp_server(
            service=WorkspaceService(factory, settings=settings),
            settings=settings,
            disk_check_provider=counted_disk_check,
        )
        args = {
            "repo_url": "git@github.com:example/docs.git",
            "base_branch": "main",
            "task_title": "Document MCP idempotency disk replay",
            "task_prompt": "Update the docs.",
            "idempotency_key": "mcp-create-v2-replay-disk",
            "provider_readiness_override": True,
            "provider_readiness_override_reason": "mcp idempotency disk test fixture",
        }

        first = await _call(mcp, "awf_create_workspace", args)
        replay = await _call(mcp, "awf_create_workspace", args)

        assert isinstance(first, dict)
        assert isinstance(replay, dict)
        assert replay["workspace_id"] == first["workspace_id"]
        assert calls == 1

    @pytest.mark.unit
    async def test_create_workspace_external_id_scope_conflict_returns_structured_error(
        self,
        mcp,
    ) -> None:  # type: ignore[no-untyped-def]
        external_id = "mcp-create-v2-external-id-conflict"
        args = {
            "repo_url": "git@github.com:example/docs.git",
            "base_branch": "main",
            "task_title": "Document external id",
            "task_prompt": "Update the docs.",
            "task_external_id": external_id,
            "provider_readiness_override": True,
            "provider_readiness_override_reason": "mcp external id conflict test fixture",
        }

        created = await _call(mcp, "awf_create_workspace", args)
        conflict = await mcp.call_tool(
            "awf_create_workspace",
            {**args, "base_branch": "release/next"},
        )

        assert isinstance(created, dict)
        assert isinstance(conflict, CallToolResult)
        assert conflict.isError is True
        assert conflict.structuredContent == {
            "error_code": "TASK_EXTERNAL_ID_CONFLICT",
            "message": (
                "External task ID is already associated with a different "
                "repo/base/task-class/owned-path scope; use a unique external "
                "task ID for this backlog slice or retry the original scope."
            ),
            "detail": {"external_id": external_id},
        }
        assert_no_internal_error_fields(conflict.structuredContent)

    @pytest.mark.unit
    async def test_retry_workspace_provider_preflight_error_and_override(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        service = WorkspaceService(
            factory,
            settings=Settings(
                _env_file=None,
                host_home=str(tmp_path / "home"),
                docker_host="",
            ),
        )
        mcp = build_mcp_server(service=service)
        created = await _call(
            mcp,
            "awf_create_workspace",
            {
                "repo_url": "git@github.com:example/retry.git",
                "base_branch": "main",
                "task_title": "Retry with provider preflight",
                "task_prompt": "Update the docs.",
                "provider_readiness_override": True,
                "provider_readiness_override_reason": "initial override",
            },
        )
        assert isinstance(created, dict)
        workspace_id = str(created["workspace_id"])
        async with factory() as session:
            repo = WorkspaceRepository(session)
            workspace = await repo.get(workspace_id)
            assert workspace is not None
            await repo.transition(workspace, to=WorkspaceStatus.provisioning, reason_code="TEST")
            await repo.transition(workspace, to=WorkspaceStatus.failed, reason_code="TEST_FAIL")
            await session.commit()

        blocked = await mcp.call_tool(
            "awf_retry_workspace",
            {"workspace_id": workspace_id},
        )
        assert isinstance(blocked, CallToolResult)
        assert blocked.isError is True
        assert blocked.structuredContent is not None
        assert blocked.structuredContent["error_code"] == "PROVIDER_READINESS_PRECHECK_FAILED"
        blocked_preflight = blocked.structuredContent["detail"]["provider_readiness_preflight"]
        assert blocked_preflight["provider"] == "codex"
        assert blocked_preflight["source_workspace_id"] == workspace_id

        retried = await _call(
            mcp,
            "awf_retry_workspace",
            {
                "workspace_id": workspace_id,
                "provider_readiness_override": True,
                "provider_readiness_override_reason": "retry override",
            },
        )

        assert isinstance(retried, dict)
        preflight = retried["provider_readiness_preflight"]
        assert preflight["source_workspace_id"] == workspace_id
        assert preflight["override_used"] is True
        assert preflight["override_reason"] == "retry override"

    @pytest.mark.unit
    async def test_retry_workspace_returns_structured_retry_error_for_missing_workspace(
        self,
        mcp,
    ) -> None:  # type: ignore[no-untyped-def]
        result = await mcp.call_tool(
            "awf_retry_workspace",
            {"workspace_id": "ws_missing_retry"},
        )

        assert isinstance(result, CallToolResult)
        assert result.isError is True
        assert result.structuredContent is not None
        assert result.structuredContent["error_code"] == "WORKSPACE_NOT_FOUND"

    @pytest.mark.unit
    async def test_observability_list_tools_return_invalid_cursor_errors(
        self,
        factory: async_sessionmaker[AsyncSession],
        mcp,
    ) -> None:  # type: ignore[no-untyped-def]
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/observability.git",
                branch_base="main",
                task_title="Observe cursor handling",
                task_prompt="Exercise invalid cursors.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()

        for tool_name in (
            "awf_list_workspace_validation",
            "awf_list_workspace_stale_reasons",
            "awf_list_workspace_artifacts",
        ):
            result = await mcp.call_tool(
                tool_name,
                {"workspace_id": workspace.id, "cursor": "not-valid-cursor"},
            )
            assert isinstance(result, CallToolResult)
            assert result.isError is True
            assert result.structuredContent is not None
            assert result.structuredContent["error_code"] == "INVALID_CURSOR"

    @pytest.mark.unit
    async def test_core_readiness_rejects_unknown_strict_provider(
        self,
        mcp,
    ) -> None:  # type: ignore[no-untyped-def]
        result = await mcp.call_tool(
            "awf_get_core_release_readiness",
            {"providers": ["bogus-provider"]},
        )

        assert isinstance(result, CallToolResult)
        assert result.isError is True
        assert result.structuredContent is not None
        assert result.structuredContent["error_code"] == "INVALID_PROVIDERS"

    @pytest.mark.unit
    async def test_unknown_profile_ref_returns_structured_invalid_profile_error(
        self,
        mcp,
    ) -> None:  # type: ignore[no-untyped-def]
        from mcp.types import CallToolResult

        result = await mcp.call_tool(
            "awf_create_workspace",
            {
                "repo_url": "git@github.com:example/app.git",
                "base_branch": "main",
                "task_title": "Add planner hook",
                "task_prompt": "Implement the planner hook.",
                "profile_ref": "missing-profile",
            },
        )

        message = "unknown workspace profile_ref: missing-profile"
        assert isinstance(result, CallToolResult)
        assert result.isError is True
        assert result.structuredContent == {
            "error_code": "INVALID_PROFILE",
            "message": message,
            "detail": None,
        }
        assert result.content[0].type == "text"


class TestGetAndList:
    @pytest.mark.unit
    async def test_get_returns_the_workspace_just_created(self, mcp) -> None:  # type: ignore[no-untyped-def]
        created = await _call(mcp, "awf_create_workspace", _CREATE_ARGS)
        ws_id = _workspace_id(created)

        fetched = await _call(mcp, "awf_get_workspace", {"workspace_id": ws_id})
        assert fetched is not None
        assert fetched["id"] == ws_id  # type: ignore[index]
        assert fetched["status"] == "requested"  # type: ignore[index]

    @pytest.mark.unit
    async def test_get_workspace_includes_issued_secret_leases(
        self,
        mcp,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:  # type: ignore[no-untyped-def]
        created = await _call(mcp, "awf_create_workspace", _CREATE_ARGS)
        ws_id = _workspace_id(created)
        raw_ref = "sk-live-do-not-appear-in-mcp"
        now = datetime(2026, 5, 16, 12, 0, tzinfo=UTC)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).get(ws_id)
            assert workspace is not None
            await SecretLeaseRepository(session).issue_declared_leases(
                workspace,
                leases=[
                    SecretLeaseIssue(
                        secret_name="api-token",
                        kind="env",
                        target="API_TOKEN",
                        mode="ro",
                        required=True,
                        provider="vault",
                        ref_digest="sha256:" + "8" * 64,
                        expires_at=now + timedelta(hours=1),
                        issue_metadata={
                            "profile": "api",
                            "declaration_index": 0,
                            "raw_ref": raw_ref,
                        },
                    )
                ],
                now=now,
            )
            await session.commit()

        fetched = await _call(mcp, "awf_get_workspace", {"workspace_id": ws_id})

        assert isinstance(fetched, dict)
        assert fetched["secret_leases"][0]["secret_name"] == "api-token"
        assert fetched["secret_leases"][0]["status"] == "issued"
        assert fetched["secret_leases"][0]["ref_digest"] == "sha256:" + "8" * 64
        assert raw_ref not in json.dumps(fetched)

    @pytest.mark.unit
    async def test_get_unknown_id_returns_none(self, mcp) -> None:  # type: ignore[no-untyped-def]
        result = await _call(mcp, "awf_get_workspace", {"workspace_id": "ws_nope"})
        assert result is None

    @pytest.mark.unit
    async def test_list_returns_newest_first(self, mcp) -> None:  # type: ignore[no-untyped-def]
        ids: list[str] = []
        for title in ["first", "second", "third"]:
            args = {**_CREATE_ARGS, "task_title": title}
            created = await _call(mcp, "awf_create_workspace", args)
            ids.append(_workspace_id(created))

        listed = await _call(mcp, "awf_list_workspaces", {"limit": 10})
        assert isinstance(listed, list)
        assert [r["id"] for r in listed] == list(reversed(ids))

    @pytest.mark.unit
    async def test_list_filters_by_status_agent_and_repo_url(
        self,
        mcp,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:  # type: ignore[no-untyped-def]
        repo_url = "git@github.com:example/filtered.git"
        matching = await _call(
            mcp,
            "awf_create_workspace",
            {
                **_CREATE_ARGS,
                "repo_url": repo_url,
                "task_title": "matching",
                "agent": "gemini",
            },
        )
        wrong_status = await _call(
            mcp,
            "awf_create_workspace",
            {
                **_CREATE_ARGS,
                "repo_url": repo_url,
                "task_title": "wrong status",
                "agent": "gemini",
            },
        )
        wrong_agent = await _call(
            mcp,
            "awf_create_workspace",
            {
                **_CREATE_ARGS,
                "repo_url": repo_url,
                "task_title": "wrong agent",
                "agent": "codex",
            },
        )
        wrong_repo = await _call(
            mcp,
            "awf_create_workspace",
            {
                **_CREATE_ARGS,
                "repo_url": "git@github.com:example/other.git",
                "task_title": "wrong repo",
                "agent": "gemini",
            },
        )
        assert isinstance(matching, dict)
        assert isinstance(wrong_status, dict)
        assert isinstance(wrong_agent, dict)
        assert isinstance(wrong_repo, dict)

        async with factory() as session:
            repo = WorkspaceRepository(session)
            for workspace_id in (
                _workspace_id(matching),
                _workspace_id(wrong_agent),
                _workspace_id(wrong_repo),
            ):
                workspace = await repo.get(str(workspace_id))
                assert workspace is not None
                await repo.transition(
                    workspace,
                    to=WorkspaceStatus.provisioning,
                    reason_code="TEST",
                )
                await repo.transition(workspace, to=WorkspaceStatus.ready, reason_code="TEST")
            await session.commit()

        listed = await _call(
            mcp,
            "awf_list_workspaces",
            {
                "status": "ready",
                "agent": "gemini",
                "repo_url": repo_url,
                "limit": 10,
            },
        )

        assert isinstance(listed, list)
        assert [row["id"] for row in listed] == [_workspace_id(matching)]
        assert _workspace_id(wrong_status) not in [row["id"] for row in listed]

    @pytest.mark.unit
    async def test_awf_list_workspaces_multi_status(
        self,
        mcp,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:  # type: ignore[no-untyped-def]
        repo_url = "git@github.com:example/filtered.git"
        ws1 = await _call(
            mcp,
            "awf_create_workspace",
            {
                **_CREATE_ARGS,
                "repo_url": repo_url,
                "task_title": "ws1",
            },
        )
        ws2 = await _call(
            mcp,
            "awf_create_workspace",
            {
                **_CREATE_ARGS,
                "repo_url": repo_url,
                "task_title": "ws2",
            },
        )
        ws3 = await _call(
            mcp,
            "awf_create_workspace",
            {
                **_CREATE_ARGS,
                "repo_url": repo_url,
                "task_title": "ws3",
            },
        )

        assert isinstance(ws1, dict)
        assert isinstance(ws2, dict)
        assert isinstance(ws3, dict)

        async with factory() as session:
            repo = WorkspaceRepository(session)
            w1 = await repo.get(str(_workspace_id(ws1)))
            assert w1 is not None
            await repo.transition(w1, to=WorkspaceStatus.provisioning, reason_code="TEST")
            await repo.transition(w1, to=WorkspaceStatus.ready, reason_code="TEST")
            await repo.transition(w1, to=WorkspaceStatus.running, reason_code="TEST")

            w2 = await repo.get(str(_workspace_id(ws2)))
            assert w2 is not None
            await repo.transition(w2, to=WorkspaceStatus.provisioning, reason_code="TEST")
            await repo.transition(w2, to=WorkspaceStatus.ready, reason_code="TEST")
            await repo.transition(w2, to=WorkspaceStatus.running, reason_code="TEST")
            await repo.transition(w2, to=WorkspaceStatus.validating, reason_code="TEST")
            await repo.transition(w2, to=WorkspaceStatus.monitoring_pr, reason_code="TEST")

            w3 = await repo.get(str(_workspace_id(ws3)))
            assert w3 is not None
            # ws3 stays at requested

            await session.commit()

        listed = await _call(
            mcp,
            "awf_list_workspaces",
            {
                "status": ["running", "monitoring_pr"],
                "limit": 10,
            },
        )

        assert isinstance(listed, list)
        listed_ids = [row["id"] for row in listed]
        assert _workspace_id(ws1) in listed_ids
        assert _workspace_id(ws2) in listed_ids
        assert _workspace_id(ws3) not in listed_ids
