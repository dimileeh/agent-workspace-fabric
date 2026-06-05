"""CLI coverage for the packaged AWF MCP server entrypoint."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import click
import pytest
from typer.testing import CliRunner

from awf.cli.main import app
from awf.service import config as service_config

_runner = CliRunner()


def _visible_help(output: str) -> str:
    """Return help text without terminal styling."""
    return click.unstyle(output)


@pytest.mark.unit
def test_awf_help_lists_mcp_group() -> None:
    """Test awf help lists mcp group."""
    result = _runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "mcp" in result.output


@pytest.mark.unit
def test_mcp_serve_help_is_available() -> None:
    """Test mcp serve help is available."""
    result = _runner.invoke(app, ["mcp", "serve", "--help"])
    visible_help = _visible_help(result.output)

    assert result.exit_code == 0
    assert "Run AWF's local MCP server over stdio" in visible_help
    assert "--env-file" in visible_help


@pytest.mark.unit
def test_mcp_serve_help_is_available_when_color_is_forced() -> None:
    """Test mcp serve help is available when CI-style color is forced."""
    result = _runner.invoke(
        app,
        ["mcp", "serve", "--help"],
        env={
            "TERM": "xterm-256color",
            "FORCE_COLOR": "1",
            "CLICOLOR_FORCE": "1",
            "GITHUB_ACTIONS": "true",
            "CI": "true",
        },
    )
    visible_help = _visible_help(result.output)

    assert result.exit_code == 0
    assert "\x1b[" in result.output
    assert "Run AWF's local MCP server over stdio" in visible_help
    assert "--env-file" in visible_help


@pytest.mark.unit
def test_mcp_serve_disposes_engine_when_server_build_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test mcp serve disposes engine when server build fails."""
    env_file = tmp_path / ".env"
    database_url = "postgresql+asyncpg://awf:awf_dev@localhost:5544/awf"
    env_file.write_text(
        "\n".join(
            [
                f"AWF_DATABASE_URL={database_url}",
                "AWF_API_TOKEN=local-dev-token",
                "AWF_HOST_WORK_DIR=/tmp/awf-mcp-test",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("AWF_DATABASE_URL", raising=False)
    monkeypatch.delenv("AWF_API_TOKEN", raising=False)
    monkeypatch.delenv("AWF_HOST_WORK_DIR", raising=False)
    calls: dict[str, Any] = {}

    class _FakeEngine:
        """Fake engine."""

        async def dispose(self) -> None:
            """Dispose."""
            calls["disposed"] = True

    def _make_engine(url: str) -> _FakeEngine:
        """Make engine."""
        calls["database_url"] = url
        return _FakeEngine()

    def _make_session_factory(engine: _FakeEngine) -> object:
        """Make session factory."""
        calls["factory_engine"] = engine
        calls["session_factory"] = object()
        return calls["session_factory"]

    def _build_mcp_server(
        *,
        service: object,
        settings: object,
        compose_env_file: object,
    ) -> object:
        """Build MCP server."""
        calls["compose_env_file"] = compose_env_file
        raise RuntimeError("boom")

    monkeypatch.setattr("awf.db.session.make_engine", _make_engine)
    monkeypatch.setattr("awf.db.session.make_session_factory", _make_session_factory)
    monkeypatch.setattr("awf.mcp.server.build_mcp_server", _build_mcp_server)

    result = _runner.invoke(app, ["mcp", "serve", "--env-file", str(env_file)])

    assert result.exit_code == 2
    assert "error: MCP server setup failed: boom" in result.stderr
    assert calls.get("disposed") is True


@pytest.mark.unit
def test_mcp_serve_runs_stdio_with_env_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test mcp serve runs stdio with env file."""
    env_file = tmp_path / ".env"
    database_url = "postgresql+asyncpg://awf:awf_dev@localhost:5544/awf"
    env_file.write_text(
        "\n".join(
            [
                f"AWF_DATABASE_URL={database_url}",
                "AWF_API_TOKEN=local-dev-token",
                "AWF_HOST_WORK_DIR=/tmp/awf-mcp-test",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("AWF_DATABASE_URL", raising=False)
    monkeypatch.delenv("AWF_API_TOKEN", raising=False)
    monkeypatch.delenv("AWF_HOST_WORK_DIR", raising=False)
    calls: dict[str, Any] = {}

    class _FakeEngine:
        """Fake engine."""

        async def dispose(self) -> None:
            """Dispose."""
            calls["disposed"] = True
            calls["dispose_loop_id"] = id(asyncio.get_running_loop())

    class _FakeMcpServer:
        """Fake MCP server."""

        async def run_stdio_async(self) -> None:
            """Run async stdio transport."""
            calls["transport"] = "stdio"
            calls["server_loop_id"] = id(asyncio.get_running_loop())

        def run(self, transport: str) -> None:
            """Run transport."""
            calls["sync_run_transport"] = transport
            asyncio.run(self.run_stdio_async())

    def _make_engine(url: str) -> _FakeEngine:
        """Make engine."""
        calls["database_url"] = url
        return _FakeEngine()

    def _make_session_factory(engine: _FakeEngine) -> object:
        """Make session factory."""
        calls["factory_engine"] = engine
        calls["session_factory"] = object()
        return calls["session_factory"]

    def _build_mcp_server(
        *,
        service: object,
        settings: object,
        compose_env_file: object,
    ) -> _FakeMcpServer:
        """Build MCP server."""
        calls["service"] = service
        calls["settings"] = settings
        calls["compose_env_file"] = compose_env_file
        return _FakeMcpServer()

    monkeypatch.setattr("awf.db.session.make_engine", _make_engine)
    monkeypatch.setattr("awf.db.session.make_session_factory", _make_session_factory)
    monkeypatch.setattr("awf.mcp.server.build_mcp_server", _build_mcp_server)

    result = _runner.invoke(app, ["mcp", "serve", "--env-file", str(env_file)])

    assert result.exit_code == 0, result.output
    assert calls["database_url"] == database_url
    assert calls["transport"] == "stdio"
    assert calls["disposed"] is True
    assert "sync_run_transport" not in calls
    assert calls["server_loop_id"] == calls["dispose_loop_id"]
    service = cast(Any, calls["service"])
    settings = cast(Any, calls["settings"])
    assert service.session_factory is calls["session_factory"]
    assert settings.database_url == database_url
    assert settings.work_dir == "/tmp/awf-mcp-test"
    assert calls["compose_env_file"] == env_file.resolve()


@pytest.mark.unit
def test_mcp_serve_rejects_missing_env_file(tmp_path: Path) -> None:
    """Test mcp serve rejects missing env file."""
    missing = tmp_path / "missing.env"

    result = _runner.invoke(app, ["mcp", "serve", "--env-file", str(missing)])

    assert result.exit_code == 2
    assert "MCP env file does not exist" in result.stderr


@pytest.mark.unit
def test_mcp_serve_runs_stdio_without_env_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test mcp serve runs stdio without an env file."""
    database_url = "postgresql+asyncpg://awf:awf_dev@localhost:5544/awf_no_env"
    monkeypatch.setenv("AWF_DATABASE_URL", database_url)
    monkeypatch.setenv("AWF_API_TOKEN", "local-dev-token")
    monkeypatch.setenv("AWF_HOST_WORK_DIR", "/tmp/awf-mcp-no-env-test")

    calls: dict[str, Any] = {}

    class _FakeEngine:
        """Fake engine."""

        async def dispose(self) -> None:
            """Dispose."""
            calls["disposed"] = True
            calls["dispose_loop_id"] = id(asyncio.get_running_loop())

    class _FakeMcpServer:
        """Fake MCP server."""

        async def run_stdio_async(self) -> None:
            """Run async stdio transport."""
            calls["transport"] = "stdio"
            calls["server_loop_id"] = id(asyncio.get_running_loop())

        def run(self, transport: str) -> None:
            """Run transport."""
            calls["sync_run_transport"] = transport
            asyncio.run(self.run_stdio_async())

    def _make_engine(url: str) -> _FakeEngine:
        """Make engine."""
        calls["database_url"] = url
        return _FakeEngine()

    def _make_session_factory(engine: _FakeEngine) -> object:
        """Make session factory."""
        calls["factory_engine"] = engine
        calls["session_factory"] = object()
        return calls["session_factory"]

    def _build_mcp_server(
        *,
        service: object,
        settings: object,
        compose_env_file: object,
    ) -> _FakeMcpServer:
        """Build MCP server."""
        calls["service"] = service
        calls["settings"] = settings
        calls["compose_env_file"] = compose_env_file
        return _FakeMcpServer()

    monkeypatch.setattr("awf.db.session.make_engine", _make_engine)
    monkeypatch.setattr("awf.db.session.make_session_factory", _make_session_factory)
    monkeypatch.setattr("awf.mcp.server.build_mcp_server", _build_mcp_server)

    result = _runner.invoke(app, ["mcp", "serve"])

    assert result.exit_code == 0, result.output
    assert calls["database_url"] == database_url
    assert calls["transport"] == "stdio"
    assert calls["disposed"] is True
    assert "sync_run_transport" not in calls
    assert calls["server_loop_id"] == calls["dispose_loop_id"]
    service = cast(Any, calls["service"])
    settings = cast(Any, calls["settings"])
    assert service.session_factory is calls["session_factory"]
    assert calls["compose_env_file"] is service_config.COMPOSE_ENV_FILE_OMITTED
    assert settings.database_url == database_url
    assert settings.work_dir == "/tmp/awf-mcp-no-env-test"
