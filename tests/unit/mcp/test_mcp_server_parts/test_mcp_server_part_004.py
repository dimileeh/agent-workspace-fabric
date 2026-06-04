"""MCP server + tool behaviour tests.

We exercise the tools via ``mcp.call_tool(name, args)`` (FastMCP's in-process
harness) against a throwaway PostgreSQL. This validates:
- All tools are registered under the expected names.
- Each tool's happy path returns the same payload shape as the REST API.
- wait_for_workspace exits on terminal state without hanging.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from mcp.types import CallToolResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.schemas import (
    OperationResponse,
    WorkspaceControlResponse,
)
from awf.common.config import Settings
from awf.db.repositories import (
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.mcp.server import WorkspaceService, build_mcp_server
from awf.service.controls import WorkspaceControlError
from awf.service.disk import DiskCheck
from tests.postgres import postgres_test_engine

_PROVIDER_AUTH_ENV_KEYS = (
    "OPENAI_API_KEY",
    "OPENAI_API_TOKEN",
    "CODEX_API_KEY",
    "CODEX_AUTH_TOKEN",
    "CURSOR_API_KEY",
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


class TestReadWorkspaceArtifact:
    @pytest.mark.unit
    async def test_tool_registered_and_bounded(self, mcp) -> None:  # type: ignore[no-untyped-def]
        tools = {tool.name: tool for tool in await mcp.list_tools()}
        assert "awf_read_workspace_artifact" in tools
        schema = tools["awf_read_workspace_artifact"].inputSchema
        props = schema["properties"]
        assert "workspace_id" in schema.get("required", [])
        assert "relative_path" in schema.get("required", [])
        assert "limit_bytes" not in schema.get("required", [])
        assert props["limit_bytes"]["default"] == 65_536
        assert props["limit_bytes"]["minimum"] == 1
        assert "maximum" not in props["limit_bytes"]

    @pytest.mark.unit
    async def test_reads_safe_small_file_and_returns_base64_content(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        settings = Settings(_env_file=None, work_dir=str(tmp_path))
        service = WorkspaceService(factory, settings=settings)
        mcp = build_mcp_server(service=service, settings=settings)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Artifact read",
                task_prompt="Read artifact.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()
        artifact_dir = tmp_path / "artifacts" / workspace.id
        artifact_dir.mkdir(parents=True)
        payload = b"hello artifact\n"
        (artifact_dir / "report.txt").write_bytes(payload)

        result = await _call(
            mcp,
            "awf_read_workspace_artifact",
            {"workspace_id": workspace.id, "relative_path": "report.txt"},
        )

        assert isinstance(result, dict)
        assert result["workspace_id"] == workspace.id
        assert result["relative_path"] == "report.txt"
        assert result["name"] == "report.txt"
        assert result["content_type"] == "text/plain"
        assert result["size_bytes"] == len(payload)
        assert base64.b64decode(result["content"]) == payload

    @pytest.mark.unit
    async def test_missing_workspace_returns_not_found(
        self,
        mcp,
    ) -> None:  # type: ignore[no-untyped-def]
        result = await mcp.call_tool(
            "awf_read_workspace_artifact",
            {"workspace_id": "ws_missing", "relative_path": "report.txt"},
        )
        assert isinstance(result, CallToolResult)
        assert result.isError is True
        assert result.structuredContent is not None
        assert result.structuredContent["error_code"] == "NOT_FOUND"

    @pytest.mark.unit
    async def test_missing_file_returns_not_found(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        settings = Settings(_env_file=None, work_dir=str(tmp_path))
        service = WorkspaceService(factory, settings=settings)
        mcp = build_mcp_server(service=service, settings=settings)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Artifact read missing",
                task_prompt="Read artifact.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()
        artifact_dir = tmp_path / "artifacts" / workspace.id
        artifact_dir.mkdir(parents=True)

        result = await mcp.call_tool(
            "awf_read_workspace_artifact",
            {"workspace_id": workspace.id, "relative_path": "missing.txt"},
        )
        assert isinstance(result, CallToolResult)
        assert result.isError is True
        assert result.structuredContent is not None
        assert result.structuredContent["error_code"] == "NOT_FOUND"
        assert "missing.txt" in result.structuredContent["message"]
        # must not leak absolute host path
        assert str(artifact_dir) not in str(result.structuredContent.get("detail", ""))

    @pytest.mark.unit
    async def test_symlink_escape_returns_not_found(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        settings = Settings(_env_file=None, work_dir=str(tmp_path))
        service = WorkspaceService(factory, settings=settings)
        mcp = build_mcp_server(service=service, settings=settings)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Artifact symlink",
                task_prompt="Read artifact.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()
        artifact_dir = tmp_path / "artifacts" / workspace.id
        artifact_dir.mkdir(parents=True)
        outside = tmp_path / "outside.txt"
        outside.write_text("secret\n", encoding="utf-8")
        (artifact_dir / "link.txt").symlink_to(outside)

        result = await mcp.call_tool(
            "awf_read_workspace_artifact",
            {"workspace_id": workspace.id, "relative_path": "link.txt"},
        )
        assert isinstance(result, CallToolResult)
        assert result.isError is True
        assert result.structuredContent is not None
        assert result.structuredContent["error_code"] == "NOT_FOUND"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "bad_path",
        [
            "../secret.txt",
            "/tmp/secret.txt",
            "",
            "reports\\summary.json",
        ],
    )
    async def test_invalid_paths_return_invalid_artifact_path(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        bad_path: str,
    ) -> None:
        settings = Settings(_env_file=None, work_dir=str(tmp_path))
        service = WorkspaceService(factory, settings=settings)
        mcp = build_mcp_server(service=service, settings=settings)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Artifact bad path",
                task_prompt="Read artifact.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()
        artifact_dir = tmp_path / "artifacts" / workspace.id
        artifact_dir.mkdir(parents=True)

        result = await mcp.call_tool(
            "awf_read_workspace_artifact",
            {"workspace_id": workspace.id, "relative_path": bad_path},
        )
        assert isinstance(result, CallToolResult)
        assert result.isError is True
        assert result.structuredContent is not None
        assert result.structuredContent["error_code"] == "INVALID_ARTIFACT_PATH"

    @pytest.mark.unit
    async def test_oversized_file_returns_artifact_oversized(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        settings = Settings(_env_file=None, work_dir=str(tmp_path))
        service = WorkspaceService(factory, settings=settings)
        mcp = build_mcp_server(service=service, settings=settings)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Artifact oversized",
                task_prompt="Read artifact.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()
        artifact_dir = tmp_path / "artifacts" / workspace.id
        artifact_dir.mkdir(parents=True)
        (artifact_dir / "big.bin").write_bytes(b"x" * 200)

        result = await mcp.call_tool(
            "awf_read_workspace_artifact",
            {"workspace_id": workspace.id, "relative_path": "big.bin", "limit_bytes": 100},
        )
        assert isinstance(result, CallToolResult)
        assert result.isError is True
        assert result.structuredContent is not None
        assert result.structuredContent["error_code"] == "ARTIFACT_OVERSIZED"
        assert result.structuredContent.get("detail") is not None
        assert isinstance(result.structuredContent["detail"], dict)
        assert result.structuredContent["detail"]["limit_bytes"] == 100
        assert result.structuredContent["detail"]["actual_bytes"] == 200

    @pytest.mark.unit
    async def test_rejects_limit_bytes_above_ceiling(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        settings = Settings(_env_file=None, work_dir=str(tmp_path))
        service = WorkspaceService(factory, settings=settings)
        mcp = build_mcp_server(service=service, settings=settings)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Artifact ceiling",
                task_prompt="Read artifact.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()
        artifact_dir = tmp_path / "artifacts" / workspace.id
        artifact_dir.mkdir(parents=True)
        (artifact_dir / "small.bin").write_bytes(b"x")

        result = await mcp.call_tool(
            "awf_read_workspace_artifact",
            {
                "workspace_id": workspace.id,
                "relative_path": "small.bin",
                "limit_bytes": 2_000_000,
            },
        )
        assert isinstance(result, CallToolResult)
        assert result.isError is True
        assert result.structuredContent is not None
        assert result.structuredContent["error_code"] == "ARTIFACT_OVERSIZED"
        assert result.structuredContent.get("detail") is not None
        assert isinstance(result.structuredContent["detail"], dict)
        assert result.structuredContent["detail"]["limit_bytes"] == 2_000_000
        assert result.structuredContent["detail"]["actual_bytes"] is None

    @pytest.mark.unit
    async def test_respects_explicit_limit_bytes_within_ceiling(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        settings = Settings(_env_file=None, work_dir=str(tmp_path))
        service = WorkspaceService(factory, settings=settings)
        mcp = build_mcp_server(service=service, settings=settings)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Artifact limit ok",
                task_prompt="Read artifact.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()
        artifact_dir = tmp_path / "artifacts" / workspace.id
        artifact_dir.mkdir(parents=True)
        payload = b"x" * 50
        (artifact_dir / "medium.bin").write_bytes(payload)

        result = await _call(
            mcp,
            "awf_read_workspace_artifact",
            {"workspace_id": workspace.id, "relative_path": "medium.bin", "limit_bytes": 100},
        )
        assert isinstance(result, dict)
        assert result["size_bytes"] == len(payload)
        assert base64.b64decode(result["content"]) == payload

    @pytest.mark.unit
    async def test_read_workspace_artifact_redacts_secrets(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        secret = "test-secret-token-abc"
        settings = Settings(_env_file=None, work_dir=str(tmp_path), api_token=secret)
        service = WorkspaceService(factory, settings=settings)
        mcp = build_mcp_server(service=service, settings=settings)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Artifact redaction",
                task_prompt="Read artifact.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()
        artifact_dir = tmp_path / "artifacts" / workspace.id
        artifact_dir.mkdir(parents=True)
        payload = f"prefix {secret} suffix".encode()
        (artifact_dir / "secret.txt").write_bytes(payload)

        result = await _call(
            mcp,
            "awf_read_workspace_artifact",
            {"workspace_id": workspace.id, "relative_path": "secret.txt", "limit_bytes": 1024},
        )
        assert isinstance(result, dict)
        decoded = base64.b64decode(result["content"])
        assert decoded == b"prefix <redacted> suffix"
        assert result["size_bytes"] == len(decoded)

    @pytest.mark.unit
    async def test_read_workspace_artifact_redacts_compose_env_file_provider_secret(
        self,
        factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Redact compose env-file provider secrets from text artifacts."""
        secret = "opaque-compose-value"
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
        compose_env_file = tmp_path / "compose.env"
        compose_env_file.write_text(f"ANTHROPIC_AUTH_TOKEN={secret}\n", encoding="utf-8")
        settings = Settings(_env_file=None, work_dir=str(tmp_path))
        service = WorkspaceService(factory, settings=settings)
        mcp = build_mcp_server(
            service=service,
            settings=settings,
            compose_env_file=compose_env_file,
        )
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Artifact Compose env redaction",
                task_prompt="Read artifact.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()
        artifact_dir = tmp_path / "artifacts" / workspace.id
        artifact_dir.mkdir(parents=True)
        (artifact_dir / "provider.txt").write_bytes(f"prefix {secret} suffix".encode())

        result = await _call(
            mcp,
            "awf_read_workspace_artifact",
            {"workspace_id": workspace.id, "relative_path": "provider.txt", "limit_bytes": 1024},
        )

        assert isinstance(result, dict)
        decoded = base64.b64decode(result["content"])
        assert decoded == b"prefix <redacted> suffix"
        assert result["size_bytes"] == len(decoded)

    @pytest.mark.unit
    async def test_read_workspace_artifact_redacts_custom_compose_env_secret(
        self,
        factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Redact bare custom secret-like Compose env values from text artifacts."""
        key = "CUSTOM_CLIENT_SECRET"
        secret = "bare-compose-custom-value"
        monkeypatch.delenv(key, raising=False)
        compose_env_file = tmp_path / "compose.env"
        compose_env_file.write_text(f"{key}={secret}\n", encoding="utf-8")
        settings = Settings(_env_file=None, work_dir=str(tmp_path))
        service = WorkspaceService(factory, settings=settings)
        mcp = build_mcp_server(
            service=service,
            settings=settings,
            compose_env_file=compose_env_file,
        )
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Artifact custom Compose env redaction",
                task_prompt="Read artifact.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()
        artifact_dir = tmp_path / "artifacts" / workspace.id
        artifact_dir.mkdir(parents=True)
        (artifact_dir / "custom-provider.txt").write_bytes(f"prefix {secret} suffix".encode())

        result = await _call(
            mcp,
            "awf_read_workspace_artifact",
            {
                "workspace_id": workspace.id,
                "relative_path": "custom-provider.txt",
                "limit_bytes": 1024,
            },
        )

        assert isinstance(result, dict)
        decoded = base64.b64decode(result["content"])
        assert decoded == b"prefix <redacted> suffix"
        assert secret.encode() not in decoded

    @pytest.mark.unit
    async def test_read_workspace_artifact_redacts_unicode_compose_env_secret(
        self,
        factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Redact non-ASCII Compose env secrets from UTF-8 text artifacts."""
        key = "ANTHROPIC_AUTH_TOKEN"
        secret = "p\u00e4ssw\u00f6rd1234"
        monkeypatch.delenv(key, raising=False)
        compose_env_file = tmp_path / "compose.env"
        compose_env_file.write_text(f"{key}={secret}\n", encoding="utf-8")
        settings = Settings(_env_file=None, work_dir=str(tmp_path))
        service = WorkspaceService(factory, settings=settings)
        mcp = build_mcp_server(
            service=service,
            settings=settings,
            compose_env_file=compose_env_file,
        )
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Artifact Unicode Compose env redaction",
                task_prompt="Read artifact.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()
        artifact_dir = tmp_path / "artifacts" / workspace.id
        artifact_dir.mkdir(parents=True)
        (artifact_dir / "unicode-provider.txt").write_bytes(f"prefix {secret} suffix".encode())

        result = await _call(
            mcp,
            "awf_read_workspace_artifact",
            {
                "workspace_id": workspace.id,
                "relative_path": "unicode-provider.txt",
                "limit_bytes": 1024,
            },
        )

        assert isinstance(result, dict)
        decoded = base64.b64decode(result["content"])
        assert decoded == b"prefix <redacted> suffix"
        assert secret.encode("utf-8") not in decoded

    @pytest.mark.unit
    async def test_read_workspace_artifact_redacts_overlapping_exact_secret_bytes(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Redact self-overlapping exact secrets before returning text artifacts."""
        secret = "abcabc"
        settings = Settings(_env_file=None, work_dir=str(tmp_path), api_token=secret)
        service = WorkspaceService(factory, settings=settings)
        mcp = build_mcp_server(service=service, settings=settings)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Artifact overlapping exact secret redaction",
                task_prompt="Read artifact.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()
        artifact_dir = tmp_path / "artifacts" / workspace.id
        artifact_dir.mkdir(parents=True)
        (artifact_dir / "overlap.txt").write_bytes(b"abcabcabc")

        result = await _call(
            mcp,
            "awf_read_workspace_artifact",
            {
                "workspace_id": workspace.id,
                "relative_path": "overlap.txt",
                "limit_bytes": 1024,
            },
        )

        assert isinstance(result, dict)
        decoded = base64.b64decode(result["content"])
        assert decoded == b"<redacted>"
        assert secret.encode() not in decoded
        assert b"abc" not in decoded

    @pytest.mark.unit
    async def test_read_workspace_artifact_coalesces_adjacent_exact_secret_bytes(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Coalesce adjacent exact secret spans before returning text artifacts."""
        api_secret = "alpha-secret-value"
        github_secret = "bravo-secret-value"
        settings = Settings(
            _env_file=None,
            work_dir=str(tmp_path),
            api_token=api_secret,
            github_token=github_secret,
        )
        service = WorkspaceService(factory, settings=settings)
        mcp = build_mcp_server(service=service, settings=settings)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Artifact adjacent exact secret redaction",
                task_prompt="Read artifact.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()
        artifact_dir = tmp_path / "artifacts" / workspace.id
        artifact_dir.mkdir(parents=True)
        (artifact_dir / "adjacent.txt").write_bytes(
            f"prefix {api_secret}{github_secret} suffix".encode()
        )

        result = await _call(
            mcp,
            "awf_read_workspace_artifact",
            {
                "workspace_id": workspace.id,
                "relative_path": "adjacent.txt",
                "limit_bytes": 1024,
            },
        )

        assert isinstance(result, dict)
        decoded = base64.b64decode(result["content"])
        assert decoded == b"prefix <redacted> suffix"
        assert api_secret.encode() not in decoded
        assert github_secret.encode() not in decoded

    @pytest.mark.unit
    async def test_read_workspace_artifact_does_not_redact_base64_content(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Secrets that happen to appear in the base64 encoding must not corrupt content."""
        secret = "SGVs"
        settings = Settings(_env_file=None, work_dir=str(tmp_path), api_token=secret)
        service = WorkspaceService(factory, settings=settings)
        mcp = build_mcp_server(service=service, settings=settings)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Artifact base64 redaction",
                task_prompt="Read artifact.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()
        artifact_dir = tmp_path / "artifacts" / workspace.id
        artifact_dir.mkdir(parents=True)
        payload = b"Hello world"
        (artifact_dir / "hello.txt").write_bytes(payload)

        result = await _call(
            mcp,
            "awf_read_workspace_artifact",
            {"workspace_id": workspace.id, "relative_path": "hello.txt", "limit_bytes": 1024},
        )
        assert isinstance(result, dict)
        decoded = base64.b64decode(result["content"])
        assert decoded == payload
        assert result["size_bytes"] == len(payload)

    @pytest.mark.unit
    async def test_utf16le_artifact_is_blocked(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        secret = "test-secret-token-abc"
        settings = Settings(_env_file=None, work_dir=str(tmp_path), api_token=secret)
        service = WorkspaceService(factory, settings=settings)
        mcp = build_mcp_server(service=service, settings=settings)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Artifact UTF-16 blocked",
                task_prompt="Read artifact.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()
        artifact_dir = tmp_path / "artifacts" / workspace.id
        artifact_dir.mkdir(parents=True)
        text = f"prefix {secret} suffix"
        payload = b"\xff\xfe" + text.encode("utf-16le")
        (artifact_dir / "secret_utf16.txt").write_bytes(payload)

        result = await _call(
            mcp,
            "awf_read_workspace_artifact",
            {
                "workspace_id": workspace.id,
                "relative_path": "secret_utf16.txt",
                "limit_bytes": 1024,
            },
        )
        assert isinstance(result, dict)
        assert result["error_code"] == "ARTIFACT_BLOCKED"

    @pytest.mark.unit
    async def test_utf16_artifact_without_mime_is_blocked(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        secret = "test-secret-token-abc"
        settings = Settings(_env_file=None, work_dir=str(tmp_path), api_token=secret)
        service = WorkspaceService(factory, settings=settings)
        mcp = build_mcp_server(service=service, settings=settings)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Artifact UTF-16 MIME-less blocked",
                task_prompt="Read artifact.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()
        artifact_dir = tmp_path / "artifacts" / workspace.id
        artifact_dir.mkdir(parents=True)
        text = f"prefix {secret} suffix"
        payload = b"\xff\xfe" + text.encode("utf-16le")
        (artifact_dir / ".env").write_bytes(payload)

        result = await _call(
            mcp,
            "awf_read_workspace_artifact",
            {
                "workspace_id": workspace.id,
                "relative_path": ".env",
                "limit_bytes": 1024,
            },
        )
        assert isinstance(result, dict)
        assert result["error_code"] == "ARTIFACT_BLOCKED"

    @pytest.mark.unit
    async def test_redaction_expansion_triggers_oversized(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        secret = "ABCD"
        settings = Settings(_env_file=None, work_dir=str(tmp_path), api_token=secret)
        service = WorkspaceService(factory, settings=settings)
        mcp = build_mcp_server(service=service, settings=settings)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Artifact redaction oversize",
                task_prompt="Read artifact.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()
        artifact_dir = tmp_path / "artifacts" / workspace.id
        artifact_dir.mkdir(parents=True)
        limit_bytes = 100
        payload = (secret * (limit_bytes // len(secret))).encode()
        (artifact_dir / "secret.txt").write_bytes(payload)

        result = await mcp.call_tool(
            "awf_read_workspace_artifact",
            {
                "workspace_id": workspace.id,
                "relative_path": "secret.txt",
                "limit_bytes": limit_bytes,
            },
        )
        assert isinstance(result, CallToolResult)
        assert result.isError is True
        assert result.structuredContent is not None
        assert result.structuredContent["error_code"] == "ARTIFACT_OVERSIZED"
        assert result.structuredContent.get("detail") is not None
        assert isinstance(result.structuredContent["detail"], dict)
        assert result.structuredContent["detail"]["limit_bytes"] == limit_bytes
        assert result.structuredContent["detail"]["actual_bytes"] == (
            (limit_bytes // len(secret)) * len("<redacted>")
        )

    @pytest.mark.unit
    async def test_binary_artifact_containing_secret_is_blocked(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        secret = "test-secret-token-abc"
        settings = Settings(_env_file=None, work_dir=str(tmp_path), api_token=secret)
        service = WorkspaceService(factory, settings=settings)
        mcp = build_mcp_server(service=service, settings=settings)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Artifact binary blocked",
                task_prompt="Read artifact.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()
        artifact_dir = tmp_path / "artifacts" / workspace.id
        artifact_dir.mkdir(parents=True)
        payload = b"\x00" + f"prefix {secret} suffix".encode() + b"\x00\xff"
        (artifact_dir / "secret.bin").write_bytes(payload)

        result = await _call(
            mcp,
            "awf_read_workspace_artifact",
            {"workspace_id": workspace.id, "relative_path": "secret.bin", "limit_bytes": 1024},
        )
        assert isinstance(result, dict)
        assert result["error_code"] == "ARTIFACT_BLOCKED"

    @pytest.mark.unit
    async def test_binary_artifact_containing_provider_env_secret_is_blocked(
        self,
        factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        secret = "env-secret-token-abc"
        monkeypatch.setenv("OPENAI_API_KEY", secret)
        settings = Settings(_env_file=None, work_dir=str(tmp_path))
        service = WorkspaceService(factory, settings=settings)
        mcp = build_mcp_server(service=service, settings=settings)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Artifact binary env secret blocked",
                task_prompt="Read artifact.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()
        artifact_dir = tmp_path / "artifacts" / workspace.id
        artifact_dir.mkdir(parents=True)
        payload = b"\x00" + f"prefix {secret} suffix".encode() + b"\x00\xff"
        (artifact_dir / "secret-env.bin").write_bytes(payload)

        result = await _call(
            mcp,
            "awf_read_workspace_artifact",
            {
                "workspace_id": workspace.id,
                "relative_path": "secret-env.bin",
                "limit_bytes": 1024,
            },
        )
        assert isinstance(result, dict)
        assert result["error_code"] == "ARTIFACT_BLOCKED"

    @pytest.mark.unit
    async def test_binary_artifact_containing_compose_env_file_provider_secret_is_blocked(
        self,
        factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Block binary artifacts containing compose env-file provider secrets."""
        secret = "opaque-compose-binary-value"
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
        compose_env_file = tmp_path / "compose.env"
        compose_env_file.write_text(f"ANTHROPIC_AUTH_TOKEN={secret}\n", encoding="utf-8")
        settings = Settings(_env_file=None, work_dir=str(tmp_path))
        service = WorkspaceService(factory, settings=settings)
        mcp = build_mcp_server(
            service=service,
            settings=settings,
            compose_env_file=compose_env_file,
        )
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Artifact binary Compose env blocked",
                task_prompt="Read artifact.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()
        artifact_dir = tmp_path / "artifacts" / workspace.id
        artifact_dir.mkdir(parents=True)
        payload = b"\x00" + f"prefix {secret} suffix".encode() + b"\x00\xff"
        (artifact_dir / "secret-compose-env.bin").write_bytes(payload)

        result = await _call(
            mcp,
            "awf_read_workspace_artifact",
            {
                "workspace_id": workspace.id,
                "relative_path": "secret-compose-env.bin",
                "limit_bytes": 1024,
            },
        )

        assert isinstance(result, dict)
        assert result["error_code"] == "ARTIFACT_BLOCKED"

    @pytest.mark.unit
    async def test_clean_binary_artifact_is_not_redacted(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        secret = "test-secret-token-abc"
        settings = Settings(_env_file=None, work_dir=str(tmp_path), api_token=secret)
        service = WorkspaceService(factory, settings=settings)
        mcp = build_mcp_server(service=service, settings=settings)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Artifact binary no redact",
                task_prompt="Read artifact.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()
        artifact_dir = tmp_path / "artifacts" / workspace.id
        artifact_dir.mkdir(parents=True)
        payload = b"\x00\xff\x01\x02\x03\x04"
        (artifact_dir / "clean.bin").write_bytes(payload)

        result = await _call(
            mcp,
            "awf_read_workspace_artifact",
            {"workspace_id": workspace.id, "relative_path": "clean.bin", "limit_bytes": 1024},
        )
        assert isinstance(result, dict)
        decoded = base64.b64decode(result["content"])
        assert decoded == payload
        assert result["size_bytes"] == len(payload)

    @pytest.mark.unit
    async def test_octet_stream_without_null_bytes_is_redacted(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        secret = "test-secret-token-abc"
        settings = Settings(_env_file=None, work_dir=str(tmp_path), api_token=secret)
        service = WorkspaceService(factory, settings=settings)
        mcp = build_mcp_server(service=service, settings=settings)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Artifact octet-stream blocked",
                task_prompt="Read artifact.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()
        artifact_dir = tmp_path / "artifacts" / workspace.id
        artifact_dir.mkdir(parents=True)
        payload = f"prefix {secret} suffix".encode()
        (artifact_dir / "secret.bin").write_bytes(payload)

        result = await _call(
            mcp,
            "awf_read_workspace_artifact",
            {"workspace_id": workspace.id, "relative_path": "secret.bin", "limit_bytes": 1024},
        )
        assert isinstance(result, dict)
        decoded = base64.b64decode(result["content"])
        assert secret.encode() not in decoded
        assert decoded == b"prefix <redacted> suffix"

    @pytest.mark.unit
    async def test_octet_stream_with_null_bytes_and_secret_is_blocked(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        secret = "test-secret-token-abc"
        settings = Settings(_env_file=None, work_dir=str(tmp_path), api_token=secret)
        service = WorkspaceService(factory, settings=settings)
        mcp = build_mcp_server(service=service, settings=settings)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Artifact octet-stream null pass",
                task_prompt="Read artifact.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()
        artifact_dir = tmp_path / "artifacts" / workspace.id
        artifact_dir.mkdir(parents=True)
        # Include a null byte + the secret: confirms binary path blocks the artifact
        # rather than silently passing it through as text-redacted content.
        payload = b"\x00" + secret.encode() + b"\x00\xff\x01\x02\x03\x04"
        (artifact_dir / "clean.bin").write_bytes(payload)

        result = await _call(
            mcp,
            "awf_read_workspace_artifact",
            {"workspace_id": workspace.id, "relative_path": "clean.bin", "limit_bytes": 1024},
        )
        assert isinstance(result, dict)
        assert result["error_code"] == "ARTIFACT_BLOCKED"

    @pytest.mark.unit
    async def test_binary_artifact_containing_provider_token_pattern_is_blocked(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        settings = Settings(_env_file=None, work_dir=str(tmp_path))
        service = WorkspaceService(factory, settings=settings)
        mcp = build_mcp_server(service=service, settings=settings)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Artifact binary token pattern blocked",
                task_prompt="Read artifact.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()
        artifact_dir = tmp_path / "artifacts" / workspace.id
        artifact_dir.mkdir(parents=True)
        # Recognizable provider token not present in settings/env
        payload = b"\x00" + b"ghp_deadbeef1234567890" + b"\x00\xff"
        (artifact_dir / "leaked.bin").write_bytes(payload)

        result = await _call(
            mcp,
            "awf_read_workspace_artifact",
            {"workspace_id": workspace.id, "relative_path": "leaked.bin", "limit_bytes": 1024},
        )
        assert isinstance(result, dict)
        assert result["error_code"] == "ARTIFACT_BLOCKED"

    @pytest.mark.unit
    async def test_binary_artifact_containing_url_credentials_is_blocked(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        settings = Settings(_env_file=None, work_dir=str(tmp_path))
        service = WorkspaceService(factory, settings=settings)
        mcp = build_mcp_server(service=service, settings=settings)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Artifact binary URL credential blocked",
                task_prompt="Read artifact.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()
        artifact_dir = tmp_path / "artifacts" / workspace.id
        artifact_dir.mkdir(parents=True)
        # URL credential not present in settings/env, wrapped in null bytes to force binary path
        payload = b"\x00" + b"https://user:password@example.com/secret" + b"\x00\xff"
        (artifact_dir / "leaked.bin").write_bytes(payload)

        result = await _call(
            mcp,
            "awf_read_workspace_artifact",
            {"workspace_id": workspace.id, "relative_path": "leaked.bin", "limit_bytes": 1024},
        )
        assert isinstance(result, dict)
        assert result["error_code"] == "ARTIFACT_BLOCKED"

    @pytest.mark.unit
    async def test_env_artifact_is_redacted(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        secret = "test-secret-token-abc"
        settings = Settings(_env_file=None, work_dir=str(tmp_path), api_token=secret)
        service = WorkspaceService(factory, settings=settings)
        mcp = build_mcp_server(service=service, settings=settings)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Artifact env redact",
                task_prompt="Read artifact.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()
        artifact_dir = tmp_path / "artifacts" / workspace.id
        artifact_dir.mkdir(parents=True)
        payload = f"API_TOKEN={secret}\n".encode()
        (artifact_dir / "config.env").write_bytes(payload)

        result = await _call(
            mcp,
            "awf_read_workspace_artifact",
            {"workspace_id": workspace.id, "relative_path": "config.env", "limit_bytes": 1024},
        )
        assert isinstance(result, dict)
        decoded = base64.b64decode(result["content"])
        assert decoded == b"API_TOKEN=<redacted>\n"
        assert result["size_bytes"] == len(decoded)

    @pytest.mark.unit
    async def test_yaml_artifact_is_redacted(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        secret = "test-secret-token-abc"
        settings = Settings(_env_file=None, work_dir=str(tmp_path), api_token=secret)
        service = WorkspaceService(factory, settings=settings)
        mcp = build_mcp_server(service=service, settings=settings)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Artifact yaml redact",
                task_prompt="Read artifact.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()
        artifact_dir = tmp_path / "artifacts" / workspace.id
        artifact_dir.mkdir(parents=True)
        payload = f"token: {secret}\n".encode()
        (artifact_dir / "values.yaml").write_bytes(payload)

        result = await _call(
            mcp,
            "awf_read_workspace_artifact",
            {"workspace_id": workspace.id, "relative_path": "values.yaml", "limit_bytes": 1024},
        )
        assert isinstance(result, dict)
        decoded = base64.b64decode(result["content"])
        assert decoded == b"token: <redacted>\n"
        assert result["size_bytes"] == len(decoded)

    @pytest.mark.unit
    async def test_json_artifact_is_redacted(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        secret = "test-secret-token-abc"
        settings = Settings(_env_file=None, work_dir=str(tmp_path), api_token=secret)
        service = WorkspaceService(factory, settings=settings)
        mcp = build_mcp_server(service=service, settings=settings)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Artifact JSON redact",
                task_prompt="Read artifact.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()
        artifact_dir = tmp_path / "artifacts" / workspace.id
        artifact_dir.mkdir(parents=True)
        payload = b'{"token": "' + secret.encode() + b'"}'
        (artifact_dir / "config.json").write_bytes(payload)

        result = await _call(
            mcp,
            "awf_read_workspace_artifact",
            {"workspace_id": workspace.id, "relative_path": "config.json", "limit_bytes": 1024},
        )
        assert isinstance(result, dict)
        decoded = base64.b64decode(result["content"])
        assert decoded == b'{"token": "<redacted>"}'
        assert result["size_bytes"] == len(decoded)
        assert result["content_type"] == "application/json"

    @pytest.mark.unit
    async def test_metadata_fields_are_redacted_when_filename_contains_secret(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        secret = "ghp_testsecret12345678"
        settings = Settings(_env_file=None, work_dir=str(tmp_path), api_token=secret)
        service = WorkspaceService(factory, settings=settings)
        mcp = build_mcp_server(service=service, settings=settings)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Artifact filename secret",
                task_prompt="Read artifact.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()
        artifact_dir = tmp_path / "artifacts" / workspace.id
        artifact_dir.mkdir(parents=True)
        relative_path = f"config/{secret}.json"
        (artifact_dir / "config").mkdir(parents=True)
        payload = b'{"ok": true}'
        (artifact_dir / relative_path).write_bytes(payload)

        result = await _call(
            mcp,
            "awf_read_workspace_artifact",
            {"workspace_id": workspace.id, "relative_path": relative_path, "limit_bytes": 1024},
        )
        assert isinstance(result, dict)
        assert result["name"] == "<redacted>.json"
        assert result["relative_path"] == "config/<redacted>.json"
        assert base64.b64decode(result["content"]) == payload

    @pytest.mark.unit
    async def test_not_found_error_redacts_secret_in_path(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        secret = "sk-proj-testsecret12345678"
        settings = Settings(_env_file=None, work_dir=str(tmp_path), api_token=secret)
        service = WorkspaceService(factory, settings=settings)
        mcp = build_mcp_server(service=service, settings=settings)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Artifact missing secret path",
                task_prompt="Read artifact.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()
        artifact_dir = tmp_path / "artifacts" / workspace.id
        artifact_dir.mkdir(parents=True)

        result = await mcp.call_tool(
            "awf_read_workspace_artifact",
            {"workspace_id": workspace.id, "relative_path": f"{secret}.txt"},
        )
        assert isinstance(result, CallToolResult)
        assert result.isError is True
        assert result.structuredContent is not None
        assert result.structuredContent["error_code"] == "NOT_FOUND"
        assert secret not in result.structuredContent["message"]
        assert "<redacted>" in result.structuredContent["message"]

    @pytest.mark.unit
    async def test_text_plain_with_null_bytes_and_secret_is_blocked(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Null bytes inside a text/* file force the binary secret-scan path.

        is_likely_text is False when null bytes are present, so the file
        must not be redacted as latin-1 text (which would corrupt UTF-16-LE
        and miss secrets). Instead it should run _contains_secret_bytes and
        be blocked when a configured secret is present.
        """
        secret = "test-secret-token-abc"
        settings = Settings(_env_file=None, work_dir=str(tmp_path), api_token=secret)
        service = WorkspaceService(factory, settings=settings)
        mcp = build_mcp_server(service=service, settings=settings)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Artifact text null secret blocked",
                task_prompt="Read artifact.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()
        artifact_dir = tmp_path / "artifacts" / workspace.id
        artifact_dir.mkdir(parents=True)
        # Null bytes make is_likely_text=False, so the binary secret-scan path
        # must run. The secret is present as contiguous ASCII bytes so
        # _contains_secret_bytes can detect it.
        payload = b"\x00" + f"prefix {secret} suffix".encode() + b"\x00\xff"
        (artifact_dir / "leak.txt").write_bytes(payload)

        result = await _call(
            mcp,
            "awf_read_workspace_artifact",
            {"workspace_id": workspace.id, "relative_path": "leak.txt", "limit_bytes": 1024},
        )
        assert isinstance(result, dict)
        assert result["error_code"] == "ARTIFACT_BLOCKED"
