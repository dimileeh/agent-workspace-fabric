"""CLI coverage for the packaged AWF MCP server entrypoint."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from typer.testing import CliRunner

from awf.cli.main import app

_runner = CliRunner()


@pytest.mark.unit
def test_awf_help_lists_mcp_group() -> None:
    result = _runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "mcp" in result.output


@pytest.mark.unit
def test_mcp_serve_help_is_available() -> None:
    result = _runner.invoke(app, ["mcp", "serve", "--help"])

    assert result.exit_code == 0
    assert "Run AWF's local MCP server over stdio" in result.output
    assert "--env-file" in result.output


@pytest.mark.unit
def test_mcp_serve_disposes_engine_when_server_build_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        async def dispose(self) -> None:
            calls["disposed"] = True

    def _make_engine(url: str) -> _FakeEngine:
        calls["database_url"] = url
        return _FakeEngine()

    def _make_session_factory(engine: _FakeEngine) -> object:
        calls["factory_engine"] = engine
        calls["session_factory"] = object()
        return calls["session_factory"]

    def _build_mcp_server(*, service: object, settings: object) -> object:
        raise RuntimeError("boom")

    monkeypatch.setattr("awf.db.session.make_engine", _make_engine)
    monkeypatch.setattr("awf.db.session.make_session_factory", _make_session_factory)
    monkeypatch.setattr("awf.mcp.server.build_mcp_server", _build_mcp_server)

    result = _runner.invoke(app, ["mcp", "serve", "--env-file", str(env_file)])

    assert result.exit_code != 0
    assert calls.get("disposed") is True


@pytest.mark.unit
def test_mcp_serve_runs_stdio_with_env_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        async def dispose(self) -> None:
            calls["disposed"] = True

    class _FakeMcpServer:
        def run(self, transport: str) -> None:
            calls["transport"] = transport

    def _make_engine(url: str) -> _FakeEngine:
        calls["database_url"] = url
        return _FakeEngine()

    def _make_session_factory(engine: _FakeEngine) -> object:
        calls["factory_engine"] = engine
        calls["session_factory"] = object()
        return calls["session_factory"]

    def _build_mcp_server(*, service: object, settings: object) -> _FakeMcpServer:
        calls["service"] = service
        calls["settings"] = settings
        return _FakeMcpServer()

    monkeypatch.setattr("awf.db.session.make_engine", _make_engine)
    monkeypatch.setattr("awf.db.session.make_session_factory", _make_session_factory)
    monkeypatch.setattr("awf.mcp.server.build_mcp_server", _build_mcp_server)

    result = _runner.invoke(app, ["mcp", "serve", "--env-file", str(env_file)])

    assert result.exit_code == 0, result.output
    assert calls["database_url"] == database_url
    assert calls["transport"] == "stdio"
    assert calls["disposed"] is True
    service = cast(Any, calls["service"])
    settings = cast(Any, calls["settings"])
    assert service.session_factory is calls["session_factory"]
    assert settings.database_url == database_url
    assert settings.work_dir == "/tmp/awf-mcp-test"


@pytest.mark.unit
def test_mcp_serve_rejects_missing_env_file(tmp_path: Path) -> None:
    missing = tmp_path / "missing.env"

    result = _runner.invoke(app, ["mcp", "serve", "--env-file", str(missing)])

    assert result.exit_code == 2
    assert "MCP env file does not exist" in result.stderr
