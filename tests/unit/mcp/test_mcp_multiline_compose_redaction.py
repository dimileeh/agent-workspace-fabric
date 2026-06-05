"""MCP multiline Compose env-file secret redaction regressions."""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.config import Settings
from awf.common.redaction import REDACTION_MARKER
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.mcp import metrics_tools as metrics_tools_mod
from awf.mcp import server as mcp_server_mod
from awf.mcp.server import WorkspaceService, build_mcp_server
from awf.runtime.logs import LogStore
from awf.service import config as service_config
from awf.service.provider_readiness import KNOWN_SECRET_ENV_KEYS
from tests.postgres import postgres_test_engine


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Yield an async session factory backed by the test Postgres database."""
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


def _write_multiline_secret_env_file(tmp_path: Path, secret: str) -> Path:
    """Write a Compose env file with a supported single-quoted multiline secret."""
    compose_env_file = tmp_path / "compose.env"
    escaped_secret = secret.replace("'", "\\'")
    compose_env_file.write_text(
        f"ANTHROPIC_AUTH_TOKEN='{escaped_secret}'\n",
        encoding="utf-8",
    )
    return compose_env_file


def _clear_known_secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the selected Compose env file as the only provider-secret source."""
    for key in (*KNOWN_SECRET_ENV_KEYS, "AWF_API_TOKEN", "AWF_GITHUB_TOKEN"):
        monkeypatch.delenv(key, raising=False)


@pytest.mark.unit
async def test_read_workspace_artifact_redacts_multiline_compose_env_file_provider_secret(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Redact every fragment of a bare multiline Compose env-file secret."""
    _clear_known_secret_env(monkeypatch)
    secret = "opaque-first-fragment\noperator's-second-fragment\nopaque-third-fragment"
    compose_env_file = _write_multiline_secret_env_file(tmp_path, secret)
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
            task_title="Artifact multiline Compose env redaction",
            task_prompt="Read artifact.",
            agent="codex",
            test_commands=[],
        )
        await session.commit()
    artifact_dir = tmp_path / "artifacts" / workspace.id
    artifact_dir.mkdir(parents=True)
    payload = f"prefix\n{secret}\nsuffix\n".encode()
    (artifact_dir / "provider.txt").write_bytes(payload)

    result = await _call(
        mcp,
        "awf_read_workspace_artifact",
        {
            "workspace_id": workspace.id,
            "relative_path": "provider.txt",
            "limit_bytes": len(payload) + 1024,
        },
    )

    assert isinstance(result, dict)
    decoded = base64.b64decode(result["content"]).decode()
    for fragment in secret.splitlines():
        assert fragment not in decoded
    assert decoded == f"prefix\n{REDACTION_MARKER}\nsuffix\n"


@pytest.mark.unit
async def test_read_workspace_log_redacts_multiline_compose_env_file_provider_secret(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Redact every fragment of a bare multiline Compose env-file log secret."""
    _clear_known_secret_env(monkeypatch)
    secret = "opaque-first-fragment\noperator's-second-fragment\nopaque-third-fragment"
    compose_env_file = _write_multiline_secret_env_file(tmp_path, secret)
    settings = Settings(_env_file=None, work_dir=str(tmp_path))
    service = WorkspaceService(factory, log_root=tmp_path / "logs", settings=settings)
    mcp = build_mcp_server(
        service=service,
        settings=settings,
        compose_env_file=compose_env_file,
    )
    async with factory() as session:
        workspace = await WorkspaceRepository(session).create(
            repo_url="git@github.com:example/app.git",
            branch_base="main",
            task_title="Log multiline Compose env redaction",
            task_prompt="Read logs.",
            agent="codex",
            test_commands=[],
        )
        await session.commit()

    raw_text = f"provider emitted\n{secret}\ndone\n"
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
    data = chunk["data"]
    for fragment in secret.splitlines():
        assert fragment not in data
    assert data == f"provider emitted\n{REDACTION_MARKER}\ndone\n"


@pytest.mark.unit
async def test_read_workspace_log_does_not_redact_multiline_first_line_fragment_outside_secret(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Do not treat a quoted multiline secret's first line as an exact secret."""
    _clear_known_secret_env(monkeypatch)
    first_line = "opaque-first-fragment"
    secret = f"{first_line}\noperator-second-fragment\nopaque-third-fragment"
    compose_env_file = _write_multiline_secret_env_file(tmp_path, secret)
    settings = Settings(_env_file=None, work_dir=str(tmp_path))
    service = WorkspaceService(factory, log_root=tmp_path / "logs", settings=settings)
    mcp = build_mcp_server(
        service=service,
        settings=settings,
        compose_env_file=compose_env_file,
    )
    async with factory() as session:
        workspace = await WorkspaceRepository(session).create(
            repo_url="git@github.com:example/app.git",
            branch_base="main",
            task_title="Log multiline first-line fragment",
            task_prompt="Read logs.",
            agent="codex",
            test_commands=[],
        )
        await session.commit()

    raw_text = f"public mention {first_line}\nprovider emitted\n{secret}\ndone\n"
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
    data = chunk["data"]
    assert first_line in data
    for fragment in secret.splitlines()[1:]:
        assert fragment not in data
    assert data == f"public mention {first_line}\nprovider emitted\n{REDACTION_MARKER}\ndone\n"


@pytest.mark.unit
async def test_register_metrics_tools_redacts_multiline_compose_env_file_provider_secret(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Standalone metrics registration collects full multiline Compose secrets."""
    _clear_known_secret_env(monkeypatch)
    first_line = "standalone-first-fragment"
    secret = f"{first_line}\nstandalone-second-fragment\nstandalone-third-fragment"
    compose_env_file = _write_multiline_secret_env_file(tmp_path, secret)
    settings = Settings(_env_file=None, work_dir=str(tmp_path))
    service = WorkspaceService(factory, log_root=tmp_path / "logs", settings=settings)
    mcp = FastMCP(name="awf-test")
    metrics_tools_mod.register_metrics_tools(
        mcp=mcp,
        service=service,
        safe_result=mcp_server_mod._tool_result,  # noqa: SLF001
        settings_value=settings,
        disk_check_provider=None,
        local_capacity_provider=None,
        orphan_resource_summary_provider=None,
        runtime_health_summary_provider=None,
        readiness_provider=None,
        health_provider=None,
        compose_env_file=compose_env_file,
    )
    async with factory() as session:
        workspace = await WorkspaceRepository(session).create(
            repo_url="git@github.com:example/app.git",
            branch_base="main",
            task_title="Standalone metrics multiline redaction",
            task_prompt="Read logs.",
            agent="codex",
            test_commands=[],
        )
        await session.commit()

    raw_text = f"public mention {first_line}\nprovider emitted\n{secret}\ndone\n"
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
    data = chunk["data"]
    assert first_line in data
    for fragment in secret.splitlines()[1:]:
        assert fragment not in data
    assert data == f"public mention {first_line}\nprovider emitted\n{REDACTION_MARKER}\ndone\n"


@pytest.mark.unit
def test_mcp_secret_values_filters_first_line_fragments_from_resolved_env_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Filter provider env first-line fragments using the fully resolved env file."""
    _clear_known_secret_env(monkeypatch)
    first_line = "resolved-first-fragment"
    secret = f"{first_line}\nresolved-second-fragment\nresolved-third-fragment"
    resolved_env_file = _write_multiline_secret_env_file(tmp_path, secret)
    shallow_env_file = tmp_path / "missing-compose.env"

    monkeypatch.setattr(
        mcp_server_mod.service_config,
        "resolve_local_service_compose_env_file",
        lambda _env_file=service_config.LOCAL_SERVICE_COMPOSE_ENV_FILE: resolved_env_file,
    )
    settings = Settings(_env_file=None, work_dir=str(tmp_path))
    service_settings = service_config.resolve_service_settings(settings, environ={})

    values = mcp_server_mod._mcp_secret_values(  # noqa: SLF001
        settings,
        service_settings,
        compose_env_file=shallow_env_file,
    )

    assert secret in values
    assert first_line not in values
