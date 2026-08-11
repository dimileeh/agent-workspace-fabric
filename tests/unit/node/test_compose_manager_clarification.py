"""Focused persisted-clarification ComposeManager regressions."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from awf.db.enums import AgentRuntime
from awf.node.compose_manager import upgrade_persisted_clarification_service


@pytest.mark.unit
def test_upgrade_persisted_clarification_service_routes_to_selected_proxy_service(
    tmp_path: Path,
) -> None:
    """Legacy clarification can reach a profile proxy service."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "proxy": {
                        "image": "squid:latest",
                        "networks": ["awf_net"],
                    },
                    "postgres": {"image": "postgres:16-alpine", "networks": ["awf_net"]},
                    "agent": {
                        "image": "awf-agent-runtime:latest",
                        "environment": {"HTTP_PROXY": "http://proxy:3128"},
                        "networks": ["awf_net"],
                    },
                },
                "networks": {"awf_net": {"name": "awf-ws_legacy-net"}},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    assert upgrade_persisted_clarification_service(
        compose_file=compose_file,
        workspace_id="ws_legacy",
        agent_runtime=AgentRuntime.claude_code,
    ) == ("proxy",)

    upgraded = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
    assert upgraded["services"]["clarification"]["networks"] == [
        "clarification_egress_net",
        "clarification_model_net",
    ]
    assert upgraded["services"]["proxy"]["networks"] == [
        "awf_net",
        "clarification_model_net",
    ]
    assert upgraded["services"]["postgres"]["networks"] == ["awf_net"]


@pytest.mark.unit
def test_upgrade_persisted_clarification_service_routes_to_container_credential_service(
    tmp_path: Path,
) -> None:
    """Legacy Bedrock clarification can reach its credential-broker service."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "credential-broker": {
                        "image": "example/credential-broker:latest",
                        "networks": ["awf_net"],
                    },
                    "postgres": {"image": "postgres:16-alpine", "networks": ["awf_net"]},
                    "agent": {
                        "image": "awf-agent-runtime:latest",
                        "environment": {
                            "CLAUDE_CODE_USE_BEDROCK": "1",
                            "AWS_REGION": "us-west-2",
                            "AWS_CONTAINER_CREDENTIALS_FULL_URI": (
                                "http://credential-broker:8080/credentials"
                            ),
                        },
                        "networks": ["awf_net"],
                    },
                },
                "networks": {"awf_net": {"name": "awf-ws_legacy-net"}},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    assert upgrade_persisted_clarification_service(
        compose_file=compose_file,
        workspace_id="ws_legacy",
        agent_runtime=AgentRuntime.claude_code,
    ) == ("credential-broker",)

    upgraded = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
    assert upgraded["services"]["clarification"]["networks"] == [
        "clarification_egress_net",
        "clarification_model_net",
    ]
    assert upgraded["services"]["credential-broker"]["networks"] == [
        "awf_net",
        "clarification_model_net",
    ]
    assert upgraded["services"]["postgres"]["networks"] == ["awf_net"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("endpoint_name", "agent_runtime", "agent_model", "model_service_name"),
    [
        pytest.param(
            "OPENAI_BASE_URL",
            AgentRuntime.codex,
            None,
            "openai-sidecar",
            id="openai",
        ),
        pytest.param(
            "ANTHROPIC_BASE_URL",
            AgentRuntime.claude_code,
            None,
            "anthropic-sidecar",
            id="anthropic",
        ),
        pytest.param(
            "AWF_OPENCODE_OLLAMA_BASE_URL",
            AgentRuntime.opencode,
            "ollama/kimi-k2.6:cloud",
            "ollama-sidecar",
            id="ollama",
        ),
    ],
)
def test_upgrade_persisted_clarification_service_resolves_host_auth_model_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    endpoint_name: str,
    agent_runtime: AgentRuntime,
    agent_model: str | None,
    model_service_name: str,
) -> None:
    """A host-auth endpoint placeholder selects its model service network."""
    compose_file = tmp_path / "compose.yml"
    monkeypatch.setenv(endpoint_name, f"http://{model_service_name}:11434")
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    model_service_name: {
                        "image": "example/model-sidecar:latest",
                        "networks": ["awf_net"],
                    },
                    "postgres": {"image": "postgres:16-alpine", "networks": ["awf_net"]},
                    "agent": {
                        "image": "awf-agent-runtime:latest",
                        "environment": {endpoint_name: f"${{{endpoint_name}}}"},
                        "networks": ["awf_net"],
                    },
                },
                "networks": {"awf_net": {"name": "awf-ws_legacy-net"}},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    assert upgrade_persisted_clarification_service(
        compose_file=compose_file,
        workspace_id="ws_legacy",
        agent_runtime=agent_runtime,
        agent_model=agent_model,
    ) == (model_service_name,)

    upgraded = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
    assert upgraded["services"]["clarification"]["networks"] == [
        "clarification_egress_net",
        "clarification_model_net",
    ]
    assert upgraded["services"][model_service_name]["networks"] == [
        "awf_net",
        "clarification_model_net",
    ]
    assert upgraded["services"]["postgres"]["networks"] == ["awf_net"]
