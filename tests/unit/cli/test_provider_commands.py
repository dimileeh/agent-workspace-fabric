"""Provider CLI sends canonical REST payloads without invoking models locally."""

import httpx
from typer.testing import CliRunner

from awf.cli.main import app


def test_provider_commands(monkeypatch):
    calls = []

    def call(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return httpx.Response(
            202,
            json={"workspace_id": "ws_diag"},
            request=httpx.Request(method, "http://api" + path),
        )

    monkeypatch.setattr("awf.cli.provider_commands._call", call)
    runner = CliRunner()
    assert (
        runner.invoke(
            app, ["provider", "models", "--agent", "codex", "--provider", "ollama"]
        ).exit_code
        == 0
    )
    assert calls[-1][1] == "/v1/providers/models/query"
    assert calls[-1][2]["json"]["provider"] == "ollama"
    result = runner.invoke(
        app,
        [
            "provider",
            "test",
            "--agent",
            "claude_code",
            "--provider",
            "openrouter",
            "--model",
            "vendor/coder",
            "--idempotency-key",
            "once",
            "--deadline-seconds",
            "240",
        ],
    )
    assert result.exit_code == 0, result.output
    assert calls[-1][1] == "/v1/providers/connection-tests"
    assert calls[-1][2]["json"]["deadline_seconds"] == 240
    assert calls[-1][2]["headers"]["Idempotency-Key"] == "once"
