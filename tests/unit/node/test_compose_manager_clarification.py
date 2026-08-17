"""Focused persisted-clarification ComposeManager regressions."""

from __future__ import annotations

import json
import os
import subprocess
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


@pytest.mark.unit
def test_upgrade_persisted_clarification_service_preserves_stack_and_filters_git_access(
    tmp_path: Path,
) -> None:
    """Legacy stacks gain only the isolated clarification service."""
    compose_file = tmp_path / "compose.yml"
    original = {
        "services": {
            "postgres": {"image": "postgres:16-alpine", "networks": ["awf_net"]},
            "agent": {
                "image": "awf-agent-runtime:latest",
                "working_dir": "/workspace",
                "environment": {
                    "WORKSPACE_ID": "ws_legacy",
                    "OPENAI_API_KEY": "${OPENAI_API_KEY}",
                    "OPENAI_BASE_URL": str(tmp_path / "mirror.git"),
                    "GITHUB_TOKEN": "${AWF_GITHUB_TOKEN}",
                    "GIT_ASKPASS": "/run/awf/secrets/bb-askpass.sh",
                    "CLAUDE_CODE_USE_BEDROCK": "1",
                    "AWS_SECRET_ACCESS_KEY": "${AWS_SECRET_ACCESS_KEY}",
                    "GOOGLE_APPLICATION_CREDENTIALS": "/run/awf/adc.json",
                },
                "volumes": [
                    f"{tmp_path / 'worktree'}:/workspace",
                    f"{tmp_path / 'mirror.git'}:{tmp_path / 'mirror.git'}:rw",
                    f"{tmp_path / 'codex'}:/home/agent/.codex:rw",
                    f"{tmp_path / 'gh'}:/home/agent/.config/gh:ro",
                    f"{tmp_path / 'gcloud'}:/home/agent/.config/gcloud:ro",
                    f"{tmp_path / 'adc.json'}:/run/awf/adc.json:ro",
                ],
                "extra_hosts": ["host.docker.internal:host-gateway"],
                "networks": ["awf_net"],
                "deploy": {"resources": {"limits": {"cpus": "4", "memory": "8g"}}},
            },
        },
        "volumes": {},
        "networks": {"awf_net": {"name": "awf-ws_legacy-net", "internal": True}},
    }
    compose_file.write_text(yaml.safe_dump(original, sort_keys=False), encoding="utf-8")

    assert (
        upgrade_persisted_clarification_service(
            compose_file=compose_file,
            workspace_id="ws_legacy",
            agent_runtime=AgentRuntime.codex,
        )
        == ()
    )

    upgraded = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
    clarification = upgraded["services"]["clarification"]
    assert upgraded["services"]["agent"] == original["services"]["agent"]
    assert upgraded["services"]["postgres"] == original["services"]["postgres"]
    assert clarification["x-awf-persisted-clarification-service-managed"] is True
    assert clarification["profiles"] == ["awf-clarification"]
    assert clarification["networks"] == ["clarification_egress_net"]
    assert clarification["extra_hosts"] == ["host.docker.internal:host-gateway"]
    assert clarification["deploy"] == original["services"]["agent"]["deploy"]
    assert clarification["environment"] == {
        "OPENAI_API_KEY": "${OPENAI_API_KEY}",
        "OPENAI_BASE_URL": str(tmp_path / "mirror.git"),
        "AWF_CLARIFICATION_AUTH_TARGET_0": "/home/agent/.codex",
    }
    assert clarification["volumes"] == [
        f"{tmp_path / 'codex'}:/run/awf/clarification-auth/0:ro",
    ]
    assert str(tmp_path / "worktree") not in "\n".join(clarification["volumes"])
    assert str(tmp_path / "mirror.git") not in "\n".join(clarification["volumes"])
    assert str(tmp_path / "gh") not in "\n".join(clarification["volumes"])
    assert "GITHUB_TOKEN" not in clarification["environment"]
    assert "GIT_ASKPASS" not in clarification["environment"]
    assert "CLAUDE_CODE_USE_BEDROCK" not in clarification["environment"]
    assert "AWS_SECRET_ACCESS_KEY" not in clarification["environment"]
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in clarification["environment"]
    assert upgraded["networks"]["clarification_egress_net"] == {
        "name": "awf-ws_legacy-clarification-egress-net",
        "internal": True,
    }
    assert not upgrade_persisted_clarification_service(
        compose_file=compose_file,
        workspace_id="ws_legacy",
        agent_runtime=AgentRuntime.codex,
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "legacy_service",
    [
        pytest.param(
            {
                "image": "example/legacy-clarification:latest",
                "environment": {"LEGACY_SERVICE_DATA": "preserve-me"},
                "networks": ["awf_net"],
            },
            id="profile-service",
        ),
        pytest.param("not-a-service", id="invalid-service"),
    ],
)
def test_upgrade_persisted_clarification_service_rejects_legacy_service_name_collision(
    tmp_path: Path,
    legacy_service: object,
) -> None:
    """A legacy profile service cannot be mistaken for AWF's managed agent."""
    compose_file = tmp_path / "compose.yml"
    original = {
        "services": {
            "agent": {"image": "awf-agent-runtime:latest"},
            "clarification": legacy_service,
        },
        "networks": {"awf_net": {"name": "awf-ws_legacy-net"}},
    }
    compose_file.write_text(yaml.safe_dump(original, sort_keys=False), encoding="utf-8")

    with pytest.raises(ValueError, match="conflicts with the managed clarification service"):
        upgrade_persisted_clarification_service(
            compose_file=compose_file,
            workspace_id="ws_legacy",
            agent_runtime=AgentRuntime.codex,
        )

    assert yaml.safe_load(compose_file.read_text(encoding="utf-8")) == original


@pytest.mark.unit
def test_upgrade_persisted_clarification_service_rerenders_after_cross_runtime_fallback(
    tmp_path: Path,
) -> None:
    """A monitor-in-place provider fallback re-renders clarification credentials.

    ``create_provider_recovery_attempt_row`` can swap a monitoring workspace's
    agent runtime without re-rendering its persisted stack, so the clarification
    container would otherwise stay frozen on the previous runtime's credentials
    and run the fallback CLI unauthenticated.
    """
    compose_file = tmp_path / "compose.yml"
    original = {
        "services": {
            "agent": {
                "image": "awf-agent-runtime:latest",
                "working_dir": "/workspace",
                "environment": {
                    "WORKSPACE_ID": "ws_fallback",
                    "OPENAI_API_KEY": "${OPENAI_API_KEY}",
                    "ANTHROPIC_API_KEY": "${ANTHROPIC_API_KEY}",
                },
                "volumes": [
                    f"{tmp_path / 'worktree'}:/workspace",
                    f"{tmp_path / 'codex'}:/home/agent/.codex:rw",
                    f"{tmp_path / 'claude'}:/home/agent/.claude:rw",
                ],
                "networks": ["awf_net"],
            },
        },
        "networks": {"awf_net": {"name": "awf-ws_fallback-net"}},
    }
    compose_file.write_text(yaml.safe_dump(original, sort_keys=False), encoding="utf-8")

    assert (
        upgrade_persisted_clarification_service(
            compose_file=compose_file,
            workspace_id="ws_fallback",
            agent_runtime=AgentRuntime.codex,
            agent_model="gpt-5.6-sol",
        )
        == ()
    )
    codex_clarification = yaml.safe_load(compose_file.read_text(encoding="utf-8"))["services"][
        "clarification"
    ]
    assert codex_clarification["volumes"] == [
        f"{tmp_path / 'codex'}:/run/awf/clarification-auth/0:ro"
    ]
    assert (
        codex_clarification["x-awf-persisted-clarification-service-runtime"] == "codex|gpt-5.6-sol"
    )

    # The workspace falls back to a different runtime in place.
    assert (
        upgrade_persisted_clarification_service(
            compose_file=compose_file,
            workspace_id="ws_fallback",
            agent_runtime=AgentRuntime.claude_code,
            agent_model="claude-opus-5",
        )
        == ()
    )

    claude_clarification = yaml.safe_load(compose_file.read_text(encoding="utf-8"))["services"][
        "clarification"
    ]
    assert claude_clarification["volumes"] == [
        f"{tmp_path / 'claude'}:/run/awf/clarification-auth/0:ro"
    ]
    assert claude_clarification["environment"]["AWF_CLARIFICATION_AUTH_TARGET_0"] == (
        "/home/agent/.claude"
    )
    assert "OPENAI_API_KEY" not in claude_clarification["environment"]
    assert (
        claude_clarification["x-awf-persisted-clarification-service-runtime"]
        == "claude_code|claude-opus-5"
    )

    # The refreshed service is stable for the runtime it now targets.
    assert (
        upgrade_persisted_clarification_service(
            compose_file=compose_file,
            workspace_id="ws_fallback",
            agent_runtime=AgentRuntime.claude_code,
            agent_model="claude-opus-5",
        )
        is None
    )


@pytest.mark.unit
def test_upgrade_persisted_clarification_service_rerender_keeps_single_model_network(
    tmp_path: Path,
) -> None:
    """Re-rendering after a model fallback must not duplicate the sidecar route."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "ollama-sidecar": {
                        "image": "ollama/ollama:latest",
                        "networks": ["awf_net"],
                    },
                    "agent": {
                        "image": "awf-agent-runtime:latest",
                        "working_dir": "/workspace",
                        "environment": {
                            "AWF_OPENCODE_OLLAMA_BASE_URL": "http://ollama-sidecar:11434"
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
        agent_runtime=AgentRuntime.opencode,
        agent_model="ollama/kimi-k2.6:cloud",
    ) == ("ollama-sidecar",)
    mark_persisted_clarification_model_network_reconciled(compose_file=compose_file)

    assert upgrade_persisted_clarification_service(
        compose_file=compose_file,
        workspace_id="ws_legacy",
        agent_runtime=AgentRuntime.opencode,
        agent_model="ollama/glm-5.1:cloud",
    ) == ("ollama-sidecar",)

    upgraded = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
    assert upgraded["services"]["ollama-sidecar"]["networks"] == [
        "awf_net",
        "clarification_model_net",
    ]
    assert upgraded["services"]["clarification"]["networks"] == [
        "clarification_egress_net",
        "clarification_model_net",
    ]


@pytest.mark.unit
def test_upgrade_persisted_clarification_service_recognizes_managed_service_signature(
    tmp_path: Path,
) -> None:
    """A previously rendered managed clarification service remains a no-op."""
    compose_file = tmp_path / "compose.yml"
    original = {
        "services": {
            "agent": {"image": "awf-agent-runtime:latest"},
            "clarification": {
                "image": "awf-agent-runtime:latest",
                "x-awf-persisted-clarification-service-managed": True,
                "networks": ["clarification_egress_net"],
                "profiles": ["awf-clarification"],
                "command": ["sh", "-c", "sleep infinity"],
                "restart": "no",
            },
        },
        "networks": {
            "clarification_egress_net": {"name": "awf-ws_legacy-clarification-egress-net"}
        },
    }
    compose_file.write_text(yaml.safe_dump(original, sort_keys=False), encoding="utf-8")

    assert (
        upgrade_persisted_clarification_service(
            compose_file=compose_file,
            workspace_id="ws_legacy",
            agent_runtime=AgentRuntime.codex,
        )
        is None
    )
    assert yaml.safe_load(compose_file.read_text(encoding="utf-8")) == original


@pytest.mark.unit
def test_upgrade_persisted_clarification_service_recognizes_unmarked_legacy_service(
    tmp_path: Path,
) -> None:
    """A pre-marker AWF clarification service remains a no-op."""
    compose_file = tmp_path / "compose.yml"
    original = {
        "services": {
            "agent": {"image": "awf-agent-runtime:latest", "working_dir": "/workspace"},
            "clarification": {
                "image": "awf-agent-runtime:latest",
                "working_dir": "/workspace",
                "networks": ["clarification_egress_net"],
                "profiles": ["awf-clarification"],
                "command": ["sh", "-c", "sleep infinity"],
                "restart": "no",
            },
        },
        "networks": {
            "clarification_egress_net": {"name": "awf-ws_legacy-clarification-egress-net"}
        },
    }
    compose_file.write_text(yaml.safe_dump(original, sort_keys=False), encoding="utf-8")

    assert (
        upgrade_persisted_clarification_service(
            compose_file=compose_file,
            workspace_id="ws_legacy",
            agent_runtime=AgentRuntime.codex,
        )
        is None
    )
    assert yaml.safe_load(compose_file.read_text(encoding="utf-8")) == original


@pytest.mark.unit
def test_upgrade_persisted_clarification_service_rejects_unmarked_signature_lookalike(
    tmp_path: Path,
) -> None:
    """A profile service cannot opt into managed behavior through shared fields."""
    compose_file = tmp_path / "compose.yml"
    original = {
        "services": {
            "agent": {"image": "awf-agent-runtime:latest"},
            "clarification": {
                "image": "profile-owned:latest",
                "environment": {"PROFILE_SECRET": "preserve-me"},
                "volumes": [f"{tmp_path / 'worktree'}:/workspace"],
                "networks": ["clarification_egress_net"],
                "profiles": ["awf-clarification"],
                "command": ["sh", "-c", "sleep infinity"],
                "restart": "no",
            },
        },
        "networks": {
            "clarification_egress_net": {"name": "awf-ws_legacy-clarification-egress-net"}
        },
    }
    compose_file.write_text(yaml.safe_dump(original, sort_keys=False), encoding="utf-8")

    with pytest.raises(ValueError, match="conflicts with the managed clarification service"):
        upgrade_persisted_clarification_service(
            compose_file=compose_file,
            workspace_id="ws_legacy",
            agent_runtime=AgentRuntime.codex,
        )

    assert yaml.safe_load(compose_file.read_text(encoding="utf-8")) == original


@pytest.mark.unit
def test_upgrade_persisted_clarification_service_keeps_opencode_model_credentials(
    tmp_path: Path,
) -> None:
    """Legacy OpenCode upgrades retain credentials for the selected provider."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "awf-agent-runtime:latest",
                        "environment": {"OPENAI_API_KEY": "${OPENAI_API_KEY}"},
                    }
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    assert (
        upgrade_persisted_clarification_service(
            compose_file=compose_file,
            workspace_id="ws_legacy",
            agent_runtime=AgentRuntime.opencode,
            agent_model="openai/gpt-5.3-codex",
        )
        == ()
    )

    upgraded = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
    assert upgraded["services"]["clarification"]["environment"] == {
        "OPENAI_API_KEY": "${OPENAI_API_KEY}"
    }


@pytest.mark.unit
def test_upgrade_persisted_clarification_service_routes_to_selected_model_service(
    tmp_path: Path,
) -> None:
    """Legacy clarification can reach only its configured model sidecar."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "ollama-sidecar": {
                        "image": "ollama/ollama:latest",
                        "networks": ["awf_net"],
                    },
                    "postgres": {"image": "postgres:16-alpine", "networks": ["awf_net"]},
                    "agent": {
                        "image": "awf-agent-runtime:latest",
                        "environment": {
                            "AWF_OPENCODE_OLLAMA_BASE_URL": "http://ollama-sidecar:11434"
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
        agent_runtime=AgentRuntime.opencode,
        agent_model="ollama/kimi-k2.6:cloud",
    ) == ("ollama-sidecar",)

    upgraded = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
    assert upgraded["services"]["clarification"]["networks"] == [
        "clarification_egress_net",
        "clarification_model_net",
    ]
    assert upgraded["services"]["ollama-sidecar"]["networks"] == [
        "awf_net",
        "clarification_model_net",
    ]
    assert upgraded["services"]["postgres"]["networks"] == ["awf_net"]
    assert upgraded["networks"]["clarification_model_net"] == {
        "name": "awf-ws_legacy-clarification-model-net",
        "internal": True,
    }
    assert upgraded["x-awf-persisted-clarification-model-network-reconciled"] is False
    assert upgrade_persisted_clarification_service(
        compose_file=compose_file,
        workspace_id="ws_legacy",
        agent_runtime=AgentRuntime.opencode,
        agent_model="ollama/kimi-k2.6:cloud",
    ) == ("ollama-sidecar",)
    mark_persisted_clarification_model_network_reconciled(compose_file=compose_file)
    mark_persisted_clarification_model_network_reconciled(compose_file=compose_file)
    assert (
        upgrade_persisted_clarification_service(
            compose_file=compose_file,
            workspace_id="ws_legacy",
            agent_runtime=AgentRuntime.opencode,
            agent_model="ollama/kimi-k2.6:cloud",
        )
        is None
    )


def test_upgrade_persisted_clarification_service_handles_unrecognized_runtime_string(
    tmp_path: Path,
) -> None:
    """Unrecognized agent runtime strings fallback to AgentRuntime.antigravity without error."""
    compose_file = tmp_path / "compose.yml"
    original = {
        "services": {
            "agent": {
                "image": "awf-agent-runtime:latest",
                "working_dir": "/workspace",
                "environment": {
                    "WORKSPACE_ID": "ws_unrecognized",
                    "GEMINI_API_KEY": "${GEMINI_API_KEY}",
                },
                "volumes": [f"{tmp_path / 'worktree'}:/workspace"],
                "networks": ["awf_net"],
            },
        },
        "networks": {"awf_net": {"name": "awf-ws_unrecognized-net"}},
    }
    compose_file.write_text(yaml.safe_dump(original, sort_keys=False), encoding="utf-8")

    assert (
        upgrade_persisted_clarification_service(
            compose_file=compose_file,
            workspace_id="ws_unrecognized",
            agent_runtime="unrecognized_custom_runtime",
        )
        == ()
    )

    upgraded = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
    clarification = upgraded["services"]["clarification"]
    assert clarification["environment"]["GEMINI_API_KEY"] == "${GEMINI_API_KEY}"


def test_upgrade_persisted_clarification_service_handles_retired_gemini_runtime(
    tmp_path: Path,
) -> None:
    """Retired AgentRuntime.gemini does not raise KeyError during clarification map lookups."""
    compose_file = tmp_path / "compose.yml"
    original = {
        "services": {
            "agent": {
                "image": "awf-agent-runtime:latest",
                "working_dir": "/workspace",
                "environment": {
                    "WORKSPACE_ID": "ws_gemini",
                },
                "volumes": [f"{tmp_path / 'worktree'}:/workspace"],
                "networks": ["awf_net"],
            },
        },
        "networks": {"awf_net": {"name": "awf-ws_gemini-net"}},
    }
    compose_file.write_text(yaml.safe_dump(original, sort_keys=False), encoding="utf-8")

    assert (
        upgrade_persisted_clarification_service(
            compose_file=compose_file,
            workspace_id="ws_gemini",
            agent_runtime=AgentRuntime.gemini,
        )
        == ()
    )

    upgraded = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
    assert "clarification" in upgraded["services"]


@pytest.mark.unit
def test_upgrade_persisted_clarification_service_stages_only_credential_files(
    tmp_path: Path,
) -> None:
    """Legacy migration applies the fresh path's per-target credential allowlist.

    Copying a whole persisted provider home would hand the reason-only
    clarification container the session history, settings, hooks, and MCP
    configuration the regular Compose entrypoint deliberately withholds.
    """
    claude_home = tmp_path / "claude"
    (claude_home / "projects").mkdir(parents=True)
    (claude_home / ".credentials.json").write_text('{"claudeAiOauth": {}}\n', encoding="utf-8")
    (claude_home / "settings.json").write_text('{"hooks": {}}\n', encoding="utf-8")
    (claude_home / "projects" / "history.jsonl").write_text("session\n", encoding="utf-8")
    claude_configuration = tmp_path / "claude.json"
    claude_configuration.write_text(
        json.dumps({"primaryApiKey": "sk-test", "mcpServers": {"untrusted": {}}}),
        encoding="utf-8",
    )
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "awf-agent-runtime:latest",
                        "environment": {"WORKSPACE_ID": "ws_legacy"},
                        "volumes": [
                            f"{claude_home}:/home/agent/.claude:rw",
                            f"{claude_configuration}:/home/agent/.claude.json:rw",
                        ],
                    }
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    upgrade_persisted_clarification_service(
        compose_file=compose_file,
        workspace_id="ws_legacy",
        agent_runtime=AgentRuntime.claude_code,
    )

    upgraded = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
    clarification = upgraded["services"]["clarification"]
    clarification_home = tmp_path / "clarification-home" / ".claude"
    script = clarification["entrypoint"][2]
    for index, volume in enumerate(clarification["volumes"]):
        source = volume.split(f":/run/awf/clarification-auth/{index}:")[0]
        script = script.replace(f"/run/awf/clarification-auth/{index}", source)
    script = script.replace("/home/agent/.claude", str(clarification_home))
    environment = os.environ | {
        name: value.replace("/home/agent/.claude", str(clarification_home))
        for name, value in clarification["environment"].items()
        if name.startswith("AWF_CLARIFICATION_AUTH_TARGET_")
    }

    subprocess.run(["sh", "-ec", script, "--", "true"], check=True, env=environment)

    assert (clarification_home / ".credentials.json").read_text(
        encoding="utf-8"
    ) == '{"claudeAiOauth": {}}\n'
    assert not (clarification_home / "settings.json").exists()
    assert not (clarification_home / "projects").exists()
    assert json.loads(
        (tmp_path / "clarification-home" / ".claude.json").read_text(encoding="utf-8")
    ) == {"primaryApiKey": "sk-test"}
