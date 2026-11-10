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
        "model_version": None,
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


@pytest.mark.parametrize("agent", ["codex", "claude_code"])
@pytest.mark.parametrize("mounted", [False, True])
def test_openrouter_probe_uses_selected_alias_and_closes_response(monkeypatch, agent, mounted):
    selected = resolve_execution(
        agent=agent,
        provider="openrouter",
        model="chosen/model",
        allow_unverified=True,
        profile={"runtime": {"environment": {"OPENROUTER_API_KEY": "${SELECTED_KEY}"}}},
    )
    response = io.BytesIO(b'{"data":[{"id":"chosen/model"}]}')
    opener = Mock()
    opener.open.return_value = response
    monkeypatch.setattr("urllib.request.build_opener", lambda *_args: opener)
    environ = {"SELECTED_KEY": "alias-credential", "OPENAI_API_KEY": "native-credential"}
    if mounted:
        environ["OPENROUTER_API_KEY"] = "mounted-credential"
    result = probe_connection(selected.model_dump(), environ)
    assert result["ok"] is True
    assert result["evidence"] == "runtime_network_readiness_only"
    request = opener.open.call_args.args[0]
    assert request.full_url == "https://openrouter.ai/api/v1/models"
    assert request.get_header("Authorization") == "Bearer " + (
        "mounted-credential" if mounted else "alias-credential"
    )
    assert opener.open.call_args.kwargs["timeout"] == 10
    assert response.closed
    assert "credential" not in str(result)


@pytest.mark.parametrize("agent", ["codex", "claude_code"])
def test_missing_selected_probe_credential_fails_before_network(monkeypatch, agent):
    selected = resolve_execution(
        agent=agent, provider="openrouter", model="chosen/model", allow_unverified=True
    )
    opener = Mock()
    monkeypatch.setattr("urllib.request.build_opener", opener)
    with pytest.raises(ValueError, match="^PROVIDER_CREDENTIAL_MISSING$"):
        probe_connection(selected.model_dump(), {"OPENAI_API_KEY": "unrelated-native-secret"})
    opener.assert_not_called()


def test_probe_read_timeout_closes_response_and_redacts_error(monkeypatch):
    class Response(io.BytesIO):
        def read(self, size):
            assert size == 2_000_001
            raise TimeoutError("synthetic-credential-in-response")

    response = Response()
    opener = Mock()
    opener.open.return_value = response
    monkeypatch.setattr("urllib.request.build_opener", lambda *_args: opener)
    selected = resolve_execution(
        agent="codex", provider="ollama", model="coder", allow_unverified=True
    )
    assert probe_connection(selected.model_dump(), {}) == {
        "ok": False,
        "reason_code": "PROVIDER_ENDPOINT_UNREACHABLE",
    }
    assert response.closed


def test_admission_defers_runtime_network_verification_without_override():
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
    assert result["blocks_launch"] is False
    assert result["override_used"] is False
    assert result["reason_code"] == "EXECUTION_NETWORK_PROBE_PENDING"
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


@pytest.mark.parametrize("pending", [False, True])
@pytest.mark.parametrize("provider", ["ollama", "openrouter"])
def test_primary_and_fallback_defer_network_verification(provider, pending):
    primary = resolve_execution(agent="codex")
    fallback = resolve_execution(
        agent="claude_code", provider=provider, model="coder", allow_unverified=True
    )
    policy = {
        "agent_execution": primary.model_dump(),
        "provider_recovery": {
            "fallbacks": [
                {
                    "agent": fallback.agent,
                    "provider": provider,
                    "model": fallback.model,
                    **({} if pending else {"agent_execution": fallback.model_dump()}),
                }
            ]
        },
        **({"agent_execution_profile_pending": True} if pending else {}),
    }
    result = selected_provider_readiness_preflight(
        Mock(), agent="codex", task_policy=policy, override=True, environ={}
    )
    assert result["blocks_launch"] is False
    assert result["override_used"] is False
    assert result["reason_code"] == "EXECUTION_NETWORK_PROBE_PENDING"
    assert result["provider"] == provider
