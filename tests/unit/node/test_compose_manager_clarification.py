"""Focused persisted-clarification ComposeManager regressions."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from awf.db.enums import AgentRuntime
from awf.node import compose_manager
from awf.node.compose_manager import (
    _legacy_bind_mount,
    mark_persisted_clarification_model_network_reconciled,
    upgrade_persisted_clarification_service,
)
from awf.node.compose_manager_clarification import (
    _attach_persisted_clarification_model_network,
    _clarification_model_service_names,
)


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


@pytest.mark.unit
@pytest.mark.parametrize(
    "mount",
    [
        pytest.param(None, id="non-string"),
        pytest.param("only-one-field", id="missing-target"),
        pytest.param("source:relative-target:ro", id="relative-target"),
    ],
)
def test_legacy_bind_mount_rejects_invalid_persisted_syntax(mount: object) -> None:
    """Legacy migration ignores malformed bind mounts instead of exposing them."""
    assert _legacy_bind_mount(mount) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    ("document", "message"),
    [
        pytest.param([], "contain a mapping", id="root-not-mapping"),
        pytest.param({"services": []}, "services mapping", id="services-not-mapping"),
        pytest.param({"services": {"agent": []}}, "agent service", id="agent-not-mapping"),
        pytest.param(
            {"services": {"agent": {"image": 3}}},
            "declare an image",
            id="image-not-string",
        ),
        pytest.param(
            {"services": {"agent": {"image": "agent"}}, "networks": []},
            "networks must be a mapping",
            id="networks-not-mapping",
        ),
    ],
)
def test_upgrade_persisted_clarification_service_rejects_invalid_legacy_documents(
    tmp_path: Path,
    document: object,
    message: str,
) -> None:
    """An invalid persisted stack fails closed before adding clarification access."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(yaml.safe_dump(document), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        upgrade_persisted_clarification_service(
            compose_file=compose_file,
            workspace_id="ws_legacy",
            agent_runtime=AgentRuntime.codex,
        )


@pytest.mark.unit
def test_upgrade_persisted_clarification_service_reports_unreadable_compose_file(
    tmp_path: Path,
) -> None:
    """Legacy migration reports an unavailable stack instead of creating a replacement."""
    missing_compose_file = tmp_path / "missing-compose.yml"

    with pytest.raises(ValueError, match="could not read persisted Compose file"):
        upgrade_persisted_clarification_service(
            compose_file=missing_compose_file,
            workspace_id="ws_legacy",
            agent_runtime=AgentRuntime.codex,
        )


@pytest.mark.unit
def test_upgrade_persisted_clarification_service_removes_partial_render_on_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A serialization failure leaves the persisted stack intact and no temp file behind."""
    compose_file = tmp_path / "compose.yml"
    original = yaml.safe_dump(
        {"services": {"agent": {"image": "awf-agent-runtime:latest"}}},
        sort_keys=False,
    )
    compose_file.write_text(original, encoding="utf-8")

    def _raise_serialization_error(*_args: object, **_kwargs: object) -> str:
        raise yaml.YAMLError("serialization failed")

    monkeypatch.setattr(compose_manager.yaml, "safe_dump", _raise_serialization_error)

    with pytest.raises(ValueError, match="could not upgrade persisted Compose file"):
        upgrade_persisted_clarification_service(
            compose_file=compose_file,
            workspace_id="ws_legacy",
            agent_runtime=AgentRuntime.codex,
        )

    assert compose_file.read_text(encoding="utf-8") == original
    assert not list(tmp_path.glob(".compose.yml.*.tmp"))


@pytest.mark.unit
def test_mark_persisted_clarification_model_network_reconciled_rejects_invalid_document(
    tmp_path: Path,
) -> None:
    """The reconciliation marker cannot be written into a non-mapping document."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text("[]\n", encoding="utf-8")

    with pytest.raises(ValueError, match="contain a mapping"):
        mark_persisted_clarification_model_network_reconciled(compose_file=compose_file)


@pytest.mark.unit
def test_mark_persisted_clarification_model_network_reconciled_cleans_partial_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed marker write retains the old Compose file and removes its temp render."""
    compose_file = tmp_path / "compose.yml"
    original = yaml.safe_dump({"services": {}}, sort_keys=False)
    compose_file.write_text(original, encoding="utf-8")

    def _raise_serialization_error(*_args: object, **_kwargs: object) -> str:
        raise yaml.YAMLError("serialization failed")

    monkeypatch.setattr(compose_manager.yaml, "safe_dump", _raise_serialization_error)

    with pytest.raises(
        ValueError, match="could not mark persisted Compose model network reconciled"
    ):
        mark_persisted_clarification_model_network_reconciled(compose_file=compose_file)

    assert compose_file.read_text(encoding="utf-8") == original
    assert not list(tmp_path.glob(".compose.yml.*.tmp"))


@pytest.mark.unit
def test_persisted_clarification_selects_ollama_host_without_url_scheme() -> None:
    """Legacy Ollama host settings select the matching sidecar network route."""
    assert _clarification_model_service_names(
        (("OLLAMA_HOST", "ollama-sidecar:11434"),),
        service_names=("ollama-sidecar", "postgres"),
    ) == ("ollama-sidecar",)


@pytest.mark.unit
def test_persisted_clarification_ignores_malformed_provider_endpoint() -> None:
    """A malformed legacy URL cannot select a service by partial hostname parsing."""
    assert (
        _clarification_model_service_names(
            (("OPENAI_BASE_URL", "http://[invalid-host"),),
            service_names=("openai-sidecar",),
        )
        == ()
    )


@pytest.mark.unit
def test_persisted_clarification_skips_unusable_model_service_definitions() -> None:
    """Only real AWF-networked sidecars are attached to the clarification network."""
    services: dict[object, object] = {
        "not-a-mapping": "sidecar",
        "missing-awf-network": {"networks": ["other_net"]},
    }

    assert (
        _attach_persisted_clarification_model_network(
            services,
            ("absent", "not-a-mapping", "missing-awf-network"),
        )
        == ()
    )
    assert services["missing-awf-network"] == {"networks": ["other_net"]}
