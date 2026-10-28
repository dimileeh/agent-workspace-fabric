"""Hosted admission probes the configured daemon without ambient credentials."""

import os
import subprocess

import pytest

from awf.common.config import get_settings
from tests.unit.test_hosted_runtime_authority import authority as authority
from tests.unit.test_hosted_runtime_review_create import (
    create_application as create_application,
)
from tests.unit.test_hosted_runtime_review_create import (
    create_client as create_client,
)


@pytest.mark.parametrize("cli_present", [True, False])
async def test_hosted_create_uses_isolated_configured_docker_transport(
    authority, create_application, create_client, monkeypatch, cli_present
):
    from awf.service import provider_readiness

    runtime, payload, _ = authority
    app, _ = create_application
    client, headers = create_client
    settings = app.dependency_overrides[get_settings]().model_copy(
        update={"docker_host": "unix:///run/qualified-execution/docker.sock"}
    )
    app.dependency_overrides[get_settings] = lambda: settings
    monkeypatch.setenv("DOCKER_HOST", "tcp://untrusted.invalid:2375")
    monkeypatch.setenv("DOCKER_CONTEXT", "untrusted-context")
    monkeypatch.setenv("DOCKER_CONFIG", "/ambient/docker-auth")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-ambient-provider-key")
    monkeypatch.setenv("HTTPS_PROXY", "https://untrusted.invalid")
    calls = []

    def probe(args, **kwargs):
        environment = kwargs["env"]
        assert environment["DOCKER_HOST"] == settings.docker_host
        assert environment["PATH"] == os.environ.get("PATH", os.defpath)
        assert environment["HOME"] == str(runtime.receiver.config.material_root / "no-host-auth")
        assert environment["LANG"] == "C.UTF-8"
        assert environment["CODEX_API_KEY"] == "synthetic-agent-key"
        assert environment["GH_TOKEN"] == "synthetic-github-key"
        assert set(environment) == {
            "PATH",
            "HOME",
            "LANG",
            "DOCKER_HOST",
            "CODEX_API_KEY",
            "GH_TOKEN",
            "GITHUB_TOKEN",
        }
        assert args[:3] == ["docker", "run", "--rm"]
        assert args[-1] == "command -v codex"
        calls.append(args)
        return subprocess.CompletedProcess(args, 0 if cli_present else 1, "synthetic-cli", "")

    monkeypatch.setattr(provider_readiness, "_run_subprocess", probe)
    response = await client.post("/v1/workspaces", json=payload, headers=headers)
    assert response.status_code == (202 if cli_present else 409)
    assert len(calls) == 1
    if not cli_present:
        assert response.json()["error_code"] == "PROVIDER_READINESS_PRECHECK_FAILED"
