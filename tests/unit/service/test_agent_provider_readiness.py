"""Bounded runtime-network readiness cannot be confused with coding evidence."""

import io
import json
import urllib.error
from unittest.mock import Mock

import pytest

from awf.adapters.provider_config import probe_connection
from awf.common.agent_execution import resolve_execution
from awf.service.provider_readiness import selected_provider_readiness_preflight


@pytest.mark.parametrize("model", ["coder", "coder:cloud"])
def test_daemon_local_and_cloud_probe_same_runtime_endpoint(monkeypatch, model):
    selected = resolve_execution(
        agent="codex",
        provider="ollama",
        model=model,
        allow_unverified=True,
        environ={"AWF_OLLAMA_BASE_URL": "http://daemon:11434/prefix/v1"},
    )
    response = io.BytesIO(json.dumps({"models": [{"name": model}]}).encode())
    opener = Mock()
    opener.open.return_value = response
    monkeypatch.setattr("urllib.request.build_opener", lambda *_args: opener)
    result = probe_connection(selected.model_dump(), {})
    assert result == {
        "ok": True,
        "reason_code": "PROVIDER_ENDPOINT_READY",
        "evidence": "runtime_network_readiness_only",
    }
    assert opener.open.call_args.args[0].full_url == "http://daemon:11434/prefix/api/tags"
    assert opener.open.call_args.kwargs["timeout"] == 10


@pytest.mark.parametrize(
    "response,expected",
    [
        (b"bad-json", "PROVIDER_RESPONSE_INVALID"),
        (b"{}", "PROVIDER_RESPONSE_INVALID"),
        (b'{"models":[{}]}', "PROVIDER_MODEL_UNAVAILABLE"),
    ],
)
def test_malformed_or_missing_model_has_explicit_disposition(monkeypatch, response, expected):
    selected = resolve_execution(
        agent="codex", provider="ollama", model="coder", allow_unverified=True
    )
    opener = Mock()
    opener.open.return_value = io.BytesIO(response)
    monkeypatch.setattr("urllib.request.build_opener", lambda *_args: opener)
    assert probe_connection(selected.model_dump(), {}) == {"ok": False, "reason_code": expected}


@pytest.mark.parametrize(
    "error,code",
    [
        (TimeoutError("secret"), "PROVIDER_ENDPOINT_UNREACHABLE"),
        (urllib.error.URLError("secret"), "PROVIDER_ENDPOINT_UNREACHABLE"),
        (urllib.error.HTTPError("http://daemon", 429, "secret", {}, None), "PROVIDER_HTTP_429"),
        (urllib.error.HTTPError("http://daemon", 503, "secret", {}, None), "PROVIDER_HTTP_503"),
    ],
)
def test_errors_preserve_reason_without_response_or_credential_values(monkeypatch, error, code):
    selected = resolve_execution(
        agent="codex", provider="ollama", model="coder", allow_unverified=True
    )
    opener = Mock()
    opener.open.side_effect = error
    monkeypatch.setattr("urllib.request.build_opener", lambda *_args: opener)
    result = probe_connection(selected.model_dump(), {})
    assert result == {"ok": False, "reason_code": code}
    assert "secret" not in str(result)


def test_foundation_admission_cannot_claim_runtime_network_success_or_override():
    selected = resolve_execution(
        agent="codex", provider="ollama", model="coder", allow_unverified=True
    )
    result = selected_provider_readiness_preflight(
        Mock(),
        agent="codex",
        task_policy={"agent_execution": selected.model_dump()},
        override=True,
        environ={},
    )
    assert result["blocks_launch"] is True
    assert result["override_used"] is False
    assert result["reason_code"] == "EXECUTION_NETWORK_PROBE_UNSUPPORTED"
    assert result["provider"] == "ollama"


def test_openrouter_runtime_probe_never_follows_credential_redirects():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            self.send_response(302)
            self.send_header("Location", "/stolen")
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    selected = resolve_execution(
        agent="codex", provider="openrouter", model="model", allow_unverified=True
    )
    selected = selected.model_copy(update={"endpoint": f"http://127.0.0.1:{server.server_port}"})
    try:
        result = probe_connection(
            selected.model_dump(), {"OPENROUTER_API_KEY": "synthetic-opaque-secret"}
        )
        assert result == {"ok": False, "reason_code": "PROVIDER_HTTP_302"}
        assert seen == ["/v1/models"]
        assert "synthetic-opaque-secret" not in str(result)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)
