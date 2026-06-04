"""MCP workspace log tool behaviour tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from mcp.types import CallToolResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.config import Settings
from awf.common.redaction import REDACTION_MARKER
from awf.db.repositories import (
    WorkspaceLogStreamRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.mcp import metrics_tools as metrics_tools_mod
from awf.mcp.server import WorkspaceService, build_mcp_server
from awf.runtime.logs import LogStore
from awf.service import config as service_config
from awf.service.provider_readiness import KNOWN_SECRET_ENV_KEYS
from tests.postgres import postgres_test_engine


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


async def _call(mcp, name, args) -> object:  # type: ignore[no-untyped-def]
    """Unwrap FastMCP's call_tool payload."""
    result = await mcp.call_tool(name, args)
    if isinstance(result, CallToolResult):
        return result.structuredContent
    _, payload = result
    if isinstance(payload, dict) and list(payload.keys()) == ["result"]:
        return payload["result"]
    return payload


def _log_redaction_context_for_settings(settings: Settings) -> int:
    service_settings = service_config.resolve_service_settings(settings)
    extra_secrets = metrics_tools_mod._workspace_log_redaction_secrets(  # noqa: SLF001
        settings,
        service_settings=service_settings,
    )
    return metrics_tools_mod._workspace_log_redaction_context_bytes(extra_secrets)  # noqa: SLF001


class TestWorkspaceLogs:
    @pytest.mark.unit
    async def test_lists_and_reads_indexed_log_streams(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        service = WorkspaceService(factory, log_root=tmp_path / "logs")
        mcp = build_mcp_server(service=service)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Observe logs",
                task_prompt="Write logs.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()

        store = LogStore(root=tmp_path / "logs", session_factory=factory)
        sink = await store.open_stream(
            workspace_id=workspace.id,
            stream_id="agent.stdout",
            source="agent",
            name="Agent stdout",
            kind="stdout",
        )
        await sink.write("alpha\nbeta\n")
        await sink.close()

        listed = await _call(
            mcp,
            "awf_list_workspace_logs",
            {"workspace_id": workspace.id},
        )
        assert isinstance(listed, dict)
        assert [stream["stream_id"] for stream in listed["items"]] == ["agent.stdout"]
        assert listed["items"][0]["byte_count"] == len("alpha\nbeta\n")
        assert listed["items"][0]["line_count"] == 2
        assert listed["limit"] == 1

        chunk = await _call(
            mcp,
            "awf_read_workspace_log",
            {
                "workspace_id": workspace.id,
                "stream_id": "agent.stdout",
                "offset": 6,
                "limit_bytes": 4,
            },
        )
        assert chunk == {
            "stream_id": "agent.stdout",
            "offset": 6,
            "next_offset": 10,
            "eof": False,
            "data": "beta",
        }

        eof = await _call(
            mcp,
            "awf_read_workspace_log",
            {
                "workspace_id": workspace.id,
                "stream_id": "agent.stdout",
                "offset": len("alpha\nbeta\n"),
                "limit_bytes": 16,
            },
        )
        assert eof == {
            "stream_id": "agent.stdout",
            "offset": len("alpha\nbeta\n"),
            "next_offset": len("alpha\nbeta\n"),
            "eof": True,
            "data": "",
        }

    @pytest.mark.unit
    async def test_read_workspace_log_uses_byte_offsets_after_multibyte_text(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Read workspace logs from byte offsets after multibyte text."""
        service = WorkspaceService(factory, log_root=tmp_path / "logs")
        mcp = build_mcp_server(service=service)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Observe logs",
                task_prompt="Write logs.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()

        prefix = "\U0001f525alpha\n"
        raw_text = f"{prefix}beta\n"
        store = LogStore(root=tmp_path / "logs", session_factory=factory)
        sink = await store.open_stream(
            workspace_id=workspace.id,
            stream_id="agent.stdout",
            source="agent",
            name="Agent stdout",
            kind="stdout",
        )
        await sink.write(raw_text)
        await sink.close()

        offset = len(prefix.encode())
        limit_bytes = len(b"beta")
        chunk = await _call(
            mcp,
            "awf_read_workspace_log",
            {
                "workspace_id": workspace.id,
                "stream_id": "agent.stdout",
                "offset": offset,
                "limit_bytes": limit_bytes,
            },
        )

        assert chunk == {
            "stream_id": "agent.stdout",
            "offset": offset,
            "next_offset": offset + limit_bytes,
            "eof": False,
            "data": "beta",
        }

    @pytest.mark.unit
    async def test_read_workspace_log_preserves_offsets_when_expanded_context_starts_inside_multibyte_character(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Preserve byte offsets when redaction context starts inside UTF-8."""
        for key in (*KNOWN_SECRET_ENV_KEYS, "AWF_API_TOKEN"):
            monkeypatch.delenv(key, raising=False)

        log_root = tmp_path / "logs"
        service = WorkspaceService(factory, log_root=log_root)
        mcp = build_mcp_server(service=service)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Observe logs",
                task_prompt="Write logs.",
                agent="codex",
                test_commands=[],
            )
            raw_log = log_root / workspace.id / "agent.stdout.log"
            raw_log.parent.mkdir(parents=True)
            prefix = "\U0001f525" + ("x" * 4095) + " "
            raw_text = f"{prefix}TARGET\n"
            raw_log.write_text(raw_text, encoding="utf-8")
            await WorkspaceLogStreamRepository(session).create_or_get(
                workspace_id=workspace.id,
                stream_id="agent.stdout",
                source="agent",
                name="Agent stdout",
                kind="stdout",
                path=str(raw_log),
            )
            await session.commit()

        offset = len(prefix.encode())
        limit_bytes = len(b"TARGET")
        chunk = await _call(
            mcp,
            "awf_read_workspace_log",
            {
                "workspace_id": workspace.id,
                "stream_id": "agent.stdout",
                "offset": offset,
                "limit_bytes": limit_bytes,
            },
        )

        assert chunk == {
            "stream_id": "agent.stdout",
            "offset": offset,
            "next_offset": offset + limit_bytes,
            "eof": False,
            "data": "TARGET",
        }

    @pytest.mark.unit
    async def test_read_workspace_log_preserves_offsets_with_invalid_utf8_before_requested_window(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Preserve raw byte offsets when invalid UTF-8 appears before the slice."""
        for key in (*KNOWN_SECRET_ENV_KEYS, "AWF_API_TOKEN", "AWF_GITHUB_TOKEN"):
            monkeypatch.delenv(key, raising=False)

        log_root = tmp_path / "logs"
        service = WorkspaceService(factory, log_root=log_root)
        mcp = build_mcp_server(service=service, settings=Settings(_env_file=None))
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Observe invalid log bytes",
                task_prompt="Write logs.",
                agent="codex",
                test_commands=[],
            )
            raw_log = log_root / workspace.id / "agent.stdout.log"
            raw_log.parent.mkdir(parents=True)
            raw_bytes = b"\xffprefix TARGET\n"
            raw_log.write_bytes(raw_bytes)
            await WorkspaceLogStreamRepository(session).create_or_get(
                workspace_id=workspace.id,
                stream_id="agent.stdout",
                source="agent",
                name="Agent stdout",
                kind="stdout",
                path=str(raw_log),
            )
            await session.commit()

        offset = raw_bytes.index(b"TARGET")
        limit_bytes = len(b"TARGET")
        chunk = await _call(
            mcp,
            "awf_read_workspace_log",
            {
                "workspace_id": workspace.id,
                "stream_id": "agent.stdout",
                "offset": offset,
                "limit_bytes": limit_bytes,
            },
        )

        assert chunk == {
            "stream_id": "agent.stdout",
            "offset": offset,
            "next_offset": offset + limit_bytes,
            "eof": False,
            "data": "TARGET",
        }

    @pytest.mark.unit
    async def test_read_workspace_log_preserves_leading_invalid_utf8_at_offset_zero(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Return leading invalid UTF-8 bytes when the caller reads from offset zero."""
        for key in (*KNOWN_SECRET_ENV_KEYS, "AWF_API_TOKEN", "AWF_GITHUB_TOKEN"):
            monkeypatch.delenv(key, raising=False)

        log_root = tmp_path / "logs"
        service = WorkspaceService(factory, log_root=log_root)
        mcp = build_mcp_server(service=service, settings=Settings(_env_file=None))
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Observe invalid leading log bytes",
                task_prompt="Write logs.",
                agent="codex",
                test_commands=[],
            )
            raw_log = log_root / workspace.id / "agent.stdout.log"
            raw_log.parent.mkdir(parents=True)
            raw_bytes = b"\x80prefix\n"
            raw_log.write_bytes(raw_bytes)
            await WorkspaceLogStreamRepository(session).create_or_get(
                workspace_id=workspace.id,
                stream_id="agent.stdout",
                source="agent",
                name="Agent stdout",
                kind="stdout",
                path=str(raw_log),
            )
            await session.commit()

        chunk = await _call(
            mcp,
            "awf_read_workspace_log",
            {
                "workspace_id": workspace.id,
                "stream_id": "agent.stdout",
                "offset": 0,
                "limit_bytes": len(raw_bytes),
            },
        )

        assert chunk == {
            "stream_id": "agent.stdout",
            "offset": 0,
            "next_offset": len(raw_bytes),
            "eof": True,
            "data": "\ufffdprefix\n",
        }

    @pytest.mark.unit
    async def test_read_workspace_log_does_not_skip_short_non_eof_expanded_read(
        self,
        factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Advance only through returned caller-window bytes on short non-EOF reads."""
        for key in (*KNOWN_SECRET_ENV_KEYS, "AWF_API_TOKEN", "AWF_GITHUB_TOKEN"):
            monkeypatch.delenv(key, raising=False)

        service = WorkspaceService(factory)

        async def short_read_log(
            workspace_id: str,
            stream_id: str,
            *,
            offset: int = 0,
            limit_bytes: int = 65_536,
            include_bytes: bool = False,
        ) -> dict[str, object]:
            """Return a short non-EOF byte chunk for cursor advancement checks."""
            assert workspace_id == "ws_short"
            assert stream_id == "agent.stdout"
            assert offset == 0
            assert limit_bytes > 10
            assert include_bytes is True
            return {
                "stream_id": stream_id,
                "offset": offset,
                "next_offset": 8,
                "eof": False,
                "text": "01234567",
                "raw_bytes": b"01234567",
            }

        monkeypatch.setattr(service, "read_log", short_read_log)
        mcp = build_mcp_server(service=service, settings=Settings(_env_file=None))

        chunk = await _call(
            mcp,
            "awf_read_workspace_log",
            {
                "workspace_id": "ws_short",
                "stream_id": "agent.stdout",
                "offset": 5,
                "limit_bytes": 10,
            },
        )

        assert chunk == {
            "stream_id": "agent.stdout",
            "offset": 5,
            "next_offset": 8,
            "eof": False,
            "data": "567",
        }

    @pytest.mark.unit
    async def test_missing_workspace_or_stream_returns_none(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        service = WorkspaceService(factory, log_root=tmp_path / "logs")
        mcp = build_mcp_server(service=service)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Observe logs",
                task_prompt="Write logs.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()

        missing_workspace = await _call(
            mcp,
            "awf_list_workspace_logs",
            {"workspace_id": "ws_missing"},
        )
        missing_stream = await _call(
            mcp,
            "awf_read_workspace_log",
            {"workspace_id": workspace.id, "stream_id": "agent.stderr"},
        )

        assert missing_workspace is None
        assert missing_stream is None

    @pytest.mark.unit
    async def test_read_workspace_log_redacts_setup_secret_refs(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Redact setup credential references returned through MCP log reads."""
        service = WorkspaceService(factory, log_root=tmp_path / "logs")
        mcp = build_mcp_server(service=service)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Observe redacted logs",
                task_prompt="Write logs.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()

        token = "ghp_mcpWorkspaceLogSecret123456"
        plain_ref = "plain-file:///home/user/.awf/secrets/codex.default"
        env_ref = "env://OPENAI_API_KEY"
        raw_text = f"setup token={token} ref={plain_ref} env={env_ref}\n"
        store = LogStore(root=tmp_path / "logs", session_factory=factory)
        sink = await store.open_stream(
            workspace_id=workspace.id,
            stream_id="setup.stdout",
            source="setup",
            name="Setup stdout",
            kind="stdout",
        )
        await sink.write(raw_text)
        await sink.close()

        chunk = await _call(
            mcp,
            "awf_read_workspace_log",
            {
                "workspace_id": workspace.id,
                "stream_id": "setup.stdout",
                "offset": 0,
                "limit_bytes": len(raw_text),
            },
        )

        assert isinstance(chunk, dict)
        assert chunk["stream_id"] == "setup.stdout"
        assert chunk["offset"] == 0
        assert int(chunk["next_offset"]) > 0
        assert chunk["eof"] is True
        data = str(chunk["data"])
        for raw in (token, plain_ref, env_ref, "/home/user/.awf/secrets/codex.default"):
            assert raw not in data
        assert "<redacted>" in data

    @pytest.mark.unit
    async def test_read_workspace_log_redacts_compose_env_provider_secret(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Redact provider tokens sourced only from the local Compose env file."""
        for key in (*KNOWN_SECRET_ENV_KEYS, "AWF_API_TOKEN", "AWF_GITHUB_TOKEN"):
            monkeypatch.delenv(key, raising=False)

        secret = "compose-only-anthropic-provider-secret"
        compose_env_file = tmp_path / "compose.env"
        compose_env_file.write_text(f"ANTHROPIC_AUTH_TOKEN={secret}\n", encoding="utf-8")
        monkeypatch.setattr(
            metrics_tools_mod.service_config,
            "resolve_local_service_compose_env_file",
            lambda _env_file=metrics_tools_mod.service_config.LOCAL_SERVICE_COMPOSE_ENV_FILE: (
                compose_env_file
            ),
        )

        service = WorkspaceService(factory, log_root=tmp_path / "logs")
        mcp = build_mcp_server(service=service, settings=Settings(_env_file=None))
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Observe Compose env redacted logs",
                task_prompt="Write logs.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()

        raw_text = f"provider emitted {secret} without assignment context\n"
        store = LogStore(root=tmp_path / "logs", session_factory=factory)
        sink = await store.open_stream(
            workspace_id=workspace.id,
            stream_id="setup.stdout",
            source="setup",
            name="Setup stdout",
            kind="stdout",
        )
        await sink.write(raw_text)
        await sink.close()

        chunk = await _call(
            mcp,
            "awf_read_workspace_log",
            {
                "workspace_id": workspace.id,
                "stream_id": "setup.stdout",
                "offset": raw_text.index(secret),
                "limit_bytes": len(secret),
            },
        )

        assert isinstance(chunk, dict)
        assert chunk["data"] == REDACTION_MARKER
        assert secret not in str(chunk["data"])

    @pytest.mark.unit
    async def test_read_workspace_log_redacts_compose_env_custom_secret(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Redact Compose-only exact secrets whose keys use service secret naming."""
        key = "CUSTOM_CLIENT_SECRET"
        assert key not in KNOWN_SECRET_ENV_KEYS
        for env_key in (*KNOWN_SECRET_ENV_KEYS, key, "AWF_API_TOKEN", "AWF_GITHUB_TOKEN"):
            monkeypatch.delenv(env_key, raising=False)

        secret = "bare-compose-custom-value"
        compose_env_file = tmp_path / "compose.env"
        compose_env_file.write_text(f"{key}={secret}\n", encoding="utf-8")
        monkeypatch.setattr(
            metrics_tools_mod.service_config,
            "resolve_local_service_compose_env_file",
            lambda _env_file=metrics_tools_mod.service_config.LOCAL_SERVICE_COMPOSE_ENV_FILE: (
                compose_env_file
            ),
        )

        service = WorkspaceService(factory, log_root=tmp_path / "logs")
        mcp = build_mcp_server(service=service, settings=Settings(_env_file=None))
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Observe Compose custom secret redaction",
                task_prompt="Write logs.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()

        raw_text = f"service emitted {secret} without assignment context\n"
        store = LogStore(root=tmp_path / "logs", session_factory=factory)
        sink = await store.open_stream(
            workspace_id=workspace.id,
            stream_id="setup.stdout",
            source="setup",
            name="Setup stdout",
            kind="stdout",
        )
        await sink.write(raw_text)
        await sink.close()

        chunk = await _call(
            mcp,
            "awf_read_workspace_log",
            {
                "workspace_id": workspace.id,
                "stream_id": "setup.stdout",
                "offset": raw_text.index(secret),
                "limit_bytes": len(secret),
            },
        )

        assert isinstance(chunk, dict)
        assert chunk["data"] == REDACTION_MARKER
        assert secret not in str(chunk["data"])

    @pytest.mark.unit
    async def test_read_workspace_log_redacts_custom_compose_env_file_provider_secret(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Redact exact provider secrets from the MCP server's selected env file."""
        for key in (*KNOWN_SECRET_ENV_KEYS, "AWF_API_TOKEN", "AWF_GITHUB_TOKEN"):
            monkeypatch.delenv(key, raising=False)

        custom_secret = "custom-compose-env-provider-secret"
        default_secret = "default-compose-env-provider-secret"
        default_env_file = tmp_path / "default.env"
        custom_env_file = tmp_path / "custom.env"
        default_env_file.write_text(
            f"ANTHROPIC_AUTH_TOKEN={default_secret}\n",
            encoding="utf-8",
        )
        custom_env_file.write_text(
            f"ANTHROPIC_AUTH_TOKEN={custom_secret}\n",
            encoding="utf-8",
        )

        def _resolve_env_file(
            env_file: Path = metrics_tools_mod.service_config.LOCAL_SERVICE_COMPOSE_ENV_FILE,
        ) -> Path | None:
            """Resolve custom and default compose env files for the redaction test."""
            return custom_env_file if env_file == custom_env_file else default_env_file

        monkeypatch.setattr(
            metrics_tools_mod.service_config,
            "resolve_local_service_compose_env_file",
            _resolve_env_file,
        )

        service = WorkspaceService(factory, log_root=tmp_path / "logs")
        mcp = build_mcp_server(
            service=service,
            settings=Settings(_env_file=None),
            compose_env_file=custom_env_file,
        )
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Observe custom Compose env redaction",
                task_prompt="Write logs.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()

        raw_text = f"provider emitted {custom_secret} without assignment context\n"
        store = LogStore(root=tmp_path / "logs", session_factory=factory)
        sink = await store.open_stream(
            workspace_id=workspace.id,
            stream_id="setup.stdout",
            source="setup",
            name="Setup stdout",
            kind="stdout",
        )
        await sink.write(raw_text)
        await sink.close()

        chunk = await _call(
            mcp,
            "awf_read_workspace_log",
            {
                "workspace_id": workspace.id,
                "stream_id": "setup.stdout",
                "offset": raw_text.index(custom_secret),
                "limit_bytes": len(custom_secret),
            },
        )

        assert isinstance(chunk, dict)
        assert chunk["data"] == REDACTION_MARKER
        assert custom_secret not in str(chunk["data"])

    @pytest.mark.unit
    async def test_read_workspace_log_redacts_slice_starting_inside_configured_secret(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Mask a log slice that starts inside a configured extra secret."""
        secret = "opaque-nonpattern-workspace-secret-value"
        log_root = tmp_path / "logs"
        service = WorkspaceService(factory, log_root=log_root)
        mcp = build_mcp_server(
            service=service,
            settings=Settings(_env_file=None, github_token=secret),
        )
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Observe redacted logs",
                task_prompt="Write logs.",
                agent="codex",
                test_commands=[],
            )
            raw_log = log_root / workspace.id / "setup.stdout.log"
            raw_log.parent.mkdir(parents=True)
            raw_text = f"setup AWF_GITHUB_TOKEN={secret} done\n"
            raw_log.write_text(raw_text, encoding="utf-8")
            await WorkspaceLogStreamRepository(session).create_or_get(
                workspace_id=workspace.id,
                stream_id="setup.stdout",
                source="setup",
                name="Setup stdout",
                kind="stdout",
                path=str(raw_log),
            )
            await session.commit()

        offset = raw_text.index("workspace")
        limit_bytes = len("workspace")
        chunk = await _call(
            mcp,
            "awf_read_workspace_log",
            {
                "workspace_id": workspace.id,
                "stream_id": "setup.stdout",
                "offset": offset,
                "limit_bytes": limit_bytes,
            },
        )

        assert isinstance(chunk, dict)
        assert chunk["offset"] == offset
        assert chunk["next_offset"] == offset + limit_bytes
        assert chunk["eof"] is False
        assert chunk["data"] == REDACTION_MARKER

    @pytest.mark.unit
    async def test_read_workspace_log_redacts_pattern_only_secret_assignment_beyond_context(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Mask a slice that starts deep inside a pattern-only assignment value."""
        log_root = tmp_path / "logs"
        service = WorkspaceService(factory, log_root=log_root)
        mcp = build_mcp_server(service=service, settings=Settings(_env_file=None))
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Observe redacted logs",
                task_prompt="Write logs.",
                agent="codex",
                test_commands=[],
            )
            raw_log = log_root / workspace.id / "setup.stdout.log"
            raw_log.parent.mkdir(parents=True)
            fragment = "deep-secret-fragment"
            raw_text = f"setup SERVICE_TOKEN={'x' * 4_500}{fragment} done\n"
            raw_log.write_text(raw_text, encoding="utf-8")
            await WorkspaceLogStreamRepository(session).create_or_get(
                workspace_id=workspace.id,
                stream_id="setup.stdout",
                source="setup",
                name="Setup stdout",
                kind="stdout",
                path=str(raw_log),
            )
            await session.commit()

        offset = raw_text.index(fragment)
        limit_bytes = len(fragment)
        chunk = await _call(
            mcp,
            "awf_read_workspace_log",
            {
                "workspace_id": workspace.id,
                "stream_id": "setup.stdout",
                "offset": offset,
                "limit_bytes": limit_bytes,
            },
        )

        assert isinstance(chunk, dict)
        assert chunk["offset"] == offset
        assert chunk["next_offset"] == offset + limit_bytes
        assert chunk["eof"] is False
        assert chunk["data"] == REDACTION_MARKER
        assert fragment not in str(chunk["data"])

    @pytest.mark.unit
    async def test_read_workspace_log_skips_lookback_when_visible_assignment_context_redacts_slice(
        self,
        factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Avoid a second log read when the current projection has assignment context."""
        for key in (*KNOWN_SECRET_ENV_KEYS, "AWF_API_TOKEN", "AWF_GITHUB_TOKEN"):
            monkeypatch.delenv(key, raising=False)

        settings = Settings(_env_file=None)
        requested_offset = 10_000
        fragment = "visible-secret-fragment"
        requested_limit_bytes = len(fragment.encode())
        redaction_context = _log_redaction_context_for_settings(settings)
        first_offset = metrics_tools_mod._workspace_log_read_offset(  # noqa: SLF001
            requested_offset=requested_offset,
            redaction_context=redaction_context,
        )
        first_read_limit = (
            requested_offset - first_offset + requested_limit_bytes + redaction_context
        )
        slice_start = requested_offset - first_offset
        assignment_prefix = b"SERVICE_TOKEN="
        raw_bytes = (
            assignment_prefix
            + (b"x" * (slice_start - len(assignment_prefix)))
            + fragment.encode()
            + b" done\n"
        )
        first_next_offset = first_offset + len(raw_bytes)
        calls: list[tuple[int, int]] = []

        service = WorkspaceService(factory)

        async def visible_assignment_read_log(
            workspace_id: str,
            stream_id: str,
            *,
            offset: int = 0,
            limit_bytes: int = 65_536,
            include_bytes: bool = False,
        ) -> dict[str, object]:
            """Return a projection whose leading fragment already has assignment context."""
            assert workspace_id == "ws_visible_assignment"
            assert stream_id == "setup.stdout"
            assert include_bytes is True
            calls.append((offset, limit_bytes))
            if len(calls) > 1:
                raise AssertionError("assignment context is already visible")
            assert offset == first_offset
            assert limit_bytes == first_read_limit
            return {
                "stream_id": stream_id,
                "offset": offset,
                "next_offset": first_next_offset,
                "eof": False,
                "text": raw_bytes.decode(),
                "raw_bytes": raw_bytes,
            }

        monkeypatch.setattr(service, "read_log", visible_assignment_read_log)
        mcp = build_mcp_server(service=service, settings=settings)

        chunk = await _call(
            mcp,
            "awf_read_workspace_log",
            {
                "workspace_id": "ws_visible_assignment",
                "stream_id": "setup.stdout",
                "offset": requested_offset,
                "limit_bytes": requested_limit_bytes,
            },
        )

        assert isinstance(chunk, dict)
        assert calls == [(first_offset, first_read_limit)]
        assert chunk["offset"] == requested_offset
        assert chunk["next_offset"] == requested_offset + requested_limit_bytes
        assert chunk["eof"] is False
        assert chunk["data"] == REDACTION_MARKER
        assert fragment not in str(chunk["data"])

    @pytest.mark.unit
    async def test_read_workspace_log_redacts_assignment_lookback_failure(
        self,
        factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Mask an unknown leading assignment fragment if lookback is short."""
        for key in (*KNOWN_SECRET_ENV_KEYS, "AWF_API_TOKEN", "AWF_GITHUB_TOKEN"):
            monkeypatch.delenv(key, raising=False)

        settings = Settings(_env_file=None)
        requested_offset = 10_000
        fragment = "leaking-assignment-tail"
        requested_limit_bytes = len(fragment.encode())
        redaction_context = _log_redaction_context_for_settings(settings)
        first_offset = metrics_tools_mod._workspace_log_read_offset(  # noqa: SLF001
            requested_offset=requested_offset,
            redaction_context=redaction_context,
        )
        first_read_limit = (
            requested_offset - first_offset + requested_limit_bytes + redaction_context
        )
        leading_bytes = b"x" * (requested_offset - first_offset)
        narrow_bytes = leading_bytes + fragment.encode() + b" done\n"
        first_next_offset = first_offset + len(narrow_bytes)
        calls: list[tuple[int, int]] = []

        service = WorkspaceService(factory)

        async def short_lookback_read_log(
            workspace_id: str,
            stream_id: str,
            *,
            offset: int = 0,
            limit_bytes: int = 65_536,
            include_bytes: bool = False,
        ) -> dict[str, object]:
            """Return a short lookback projection for redaction fallback checks."""
            assert workspace_id == "ws_lookback_short"
            assert stream_id == "setup.stdout"
            assert include_bytes is True
            calls.append((offset, limit_bytes))
            if len(calls) == 1:
                assert offset == first_offset
                return {
                    "stream_id": stream_id,
                    "offset": offset,
                    "next_offset": first_next_offset,
                    "eof": False,
                    "text": narrow_bytes.decode(),
                    "raw_bytes": narrow_bytes,
                }
            if len(calls) == 2:
                assert offset == 0
                return {
                    "stream_id": stream_id,
                    "offset": offset,
                    "next_offset": requested_offset + requested_limit_bytes - 1,
                    "eof": False,
                    "text": "SERVICE_TOKEN=short",
                    "raw_bytes": b"SERVICE_TOKEN=short",
                }
            raise AssertionError("unexpected read_log call")

        monkeypatch.setattr(service, "read_log", short_lookback_read_log)
        mcp = build_mcp_server(service=service, settings=settings)

        chunk = await _call(
            mcp,
            "awf_read_workspace_log",
            {
                "workspace_id": "ws_lookback_short",
                "stream_id": "setup.stdout",
                "offset": requested_offset,
                "limit_bytes": requested_limit_bytes,
            },
        )

        assert isinstance(chunk, dict)
        assert calls == [
            (first_offset, first_read_limit),
            (0, first_next_offset),
        ]
        assert chunk["offset"] == requested_offset
        assert chunk["next_offset"] == requested_offset + requested_limit_bytes
        assert chunk["eof"] is False
        assert chunk["data"] == REDACTION_MARKER
        assert fragment not in str(chunk["data"])

    @pytest.mark.unit
    async def test_read_workspace_log_redacts_assignment_lookback_still_mid_fragment(
        self,
        factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Keep masking if assignment lookback still starts inside a long value."""
        for key in (*KNOWN_SECRET_ENV_KEYS, "AWF_API_TOKEN", "AWF_GITHUB_TOKEN"):
            monkeypatch.delenv(key, raising=False)

        settings = Settings(_env_file=None)
        requested_offset = 100_000
        fragment = "still-leaking-assignment-tail"
        requested_limit_bytes = len(fragment.encode())
        redaction_context = _log_redaction_context_for_settings(settings)
        lookback_bytes = metrics_tools_mod._LOG_REDACTION_ASSIGNMENT_LOOKBACK_BYTES  # noqa: SLF001
        first_offset = metrics_tools_mod._workspace_log_read_offset(  # noqa: SLF001
            requested_offset=requested_offset,
            redaction_context=redaction_context,
        )
        first_read_limit = (
            requested_offset - first_offset + requested_limit_bytes + redaction_context
        )
        lookback_offset = first_offset - lookback_bytes
        first_bytes = (b"x" * (requested_offset - first_offset)) + fragment.encode() + b" done\n"
        lookback_result_bytes = (
            (b"x" * (requested_offset - lookback_offset)) + fragment.encode() + b" done\n"
        )
        first_next_offset = first_offset + len(first_bytes)
        calls: list[tuple[int, int]] = []

        service = WorkspaceService(factory)

        async def still_mid_fragment_read_log(
            workspace_id: str,
            stream_id: str,
            *,
            offset: int = 0,
            limit_bytes: int = 65_536,
            include_bytes: bool = False,
        ) -> dict[str, object]:
            """Return a covering lookback that still lacks the assignment key."""
            assert workspace_id == "ws_lookback_mid_fragment"
            assert stream_id == "setup.stdout"
            assert include_bytes is True
            calls.append((offset, limit_bytes))
            if len(calls) == 1:
                assert offset == first_offset
                return {
                    "stream_id": stream_id,
                    "offset": offset,
                    "next_offset": first_next_offset,
                    "eof": False,
                    "text": first_bytes.decode(),
                    "raw_bytes": first_bytes,
                }
            if len(calls) == 2:
                assert offset == lookback_offset
                assert limit_bytes == first_next_offset - lookback_offset
                return {
                    "stream_id": stream_id,
                    "offset": offset,
                    "next_offset": first_next_offset,
                    "eof": False,
                    "text": lookback_result_bytes.decode(),
                    "raw_bytes": lookback_result_bytes,
                }
            raise AssertionError("unexpected read_log call")

        monkeypatch.setattr(service, "read_log", still_mid_fragment_read_log)
        mcp = build_mcp_server(service=service, settings=settings)

        chunk = await _call(
            mcp,
            "awf_read_workspace_log",
            {
                "workspace_id": "ws_lookback_mid_fragment",
                "stream_id": "setup.stdout",
                "offset": requested_offset,
                "limit_bytes": requested_limit_bytes,
            },
        )

        assert isinstance(chunk, dict)
        assert calls == [
            (first_offset, first_read_limit),
            (lookback_offset, first_next_offset - lookback_offset),
        ]
        assert chunk["offset"] == requested_offset
        assert chunk["next_offset"] == requested_offset + requested_limit_bytes
        assert chunk["eof"] is False
        assert chunk["data"] == REDACTION_MARKER
        assert fragment not in str(chunk["data"])

    @pytest.mark.unit
    async def test_read_workspace_log_preserves_long_benign_token_without_assignment_context(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Preserve ordinary long tokens when no secret assignment prefix is found."""
        log_root = tmp_path / "logs"
        service = WorkspaceService(factory, log_root=log_root)
        mcp = build_mcp_server(service=service, settings=Settings(_env_file=None))
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Observe readable logs",
                task_prompt="Write logs.",
                agent="codex",
                test_commands=[],
            )
            raw_log = log_root / workspace.id / "agent.stdout.log"
            raw_log.parent.mkdir(parents=True)
            fragment = "ordinary-fragment"
            raw_text = f"{'a' * 4_500}{fragment} done\n"
            raw_log.write_text(raw_text, encoding="utf-8")
            await WorkspaceLogStreamRepository(session).create_or_get(
                workspace_id=workspace.id,
                stream_id="agent.stdout",
                source="agent",
                name="Agent stdout",
                kind="stdout",
                path=str(raw_log),
            )
            await session.commit()

        offset = raw_text.index(fragment)
        limit_bytes = len(fragment)
        chunk = await _call(
            mcp,
            "awf_read_workspace_log",
            {
                "workspace_id": workspace.id,
                "stream_id": "agent.stdout",
                "offset": offset,
                "limit_bytes": limit_bytes,
            },
        )

        assert isinstance(chunk, dict)
        assert chunk["offset"] == offset
        assert chunk["next_offset"] == offset + limit_bytes
        assert chunk["eof"] is False
        assert chunk["data"] == fragment
