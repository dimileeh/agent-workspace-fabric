"""Frozen provider identity and connection scoping contracts."""

import pytest

from awf.common.agent_execution import (
    ExecutionSelection,
    circuit_key,
    execution_from_policy,
    resolve_execution,
)


def test_native_defaults_remain_xhigh():
    native = resolve_execution(agent="codex")
    assert native.provider == "openai"
    assert native.effort == "xhigh"
    assert native.model
    assert resolve_execution(agent="claude_code").effort == "xhigh"
    assert execution_from_policy({}) is None


@pytest.mark.parametrize("agent", ["codex", "claude_code"])
def test_third_party_requires_model_and_experimental_opt_in(agent):
    with pytest.raises(ValueError, match="MODEL_REQUIRED"):
        resolve_execution(agent=agent, provider="ollama")
    with pytest.raises(ValueError, match="MODEL_UNVERIFIED"):
        resolve_execution(agent=agent, provider="ollama", model="qwen3-coder")
    selected = resolve_execution(
        agent=agent, provider="ollama", model="qwen3-coder", allow_unverified=True
    )
    assert selected.effort is None
    assert selected.compatibility == "unverified"
    assert selected.endpoint == "http://host.docker.internal:11434"
    with pytest.raises(ValueError, match="EFFORT_UNSUPPORTED"):
        resolve_execution(
            agent=agent,
            provider="ollama",
            model="qwen3-coder",
            effort="xhigh",
            allow_unverified=True,
        )
    with pytest.raises(ValueError, match="MODEL_INCOMPATIBLE"):
        resolve_execution(
            agent=agent, provider="ollama", model="nomic-embed-text", allow_unverified=True
        )


def test_connection_precedence_normalization_and_scope():
    selection = resolve_execution(
        agent="codex",
        provider="ollama",
        model="qwen3-coder",
        allow_unverified=True,
        environ={"AWF_OLLAMA_BASE_URL": "http://service:11434"},
        profile={
            "runtime": {"environment": {"OLLAMA_HOST": "http://OLLAMA:11434/prefix/v1/"}},
            "services": {"ollama": {}},
        },
    )
    assert selection.endpoint == "http://ollama:11434/prefix"
    assert selection.network_service == "ollama"
    assert circuit_key(
        "codex", {"agent_execution": selection.model_dump()}, workspace_id="a"
    ) != circuit_key("codex", {"agent_execution": selection.model_dump()}, workspace_id="b")
    shared = selection.model_copy(update={"network_service": None})
    assert circuit_key(
        "codex", {"agent_execution": shared.model_dump()}, workspace_id="a"
    ) == circuit_key("codex", {"agent_execution": shared.model_dump()}, workspace_id="b")
    assert circuit_key("codex", {"agent_model": "gpt-test"}) == ("openai", "gpt-test")


@pytest.mark.parametrize(
    "url",
    [
        "http://u:secret@host:11434",
        "http://host?token=secret",
        "ftp://host",
        "http://",
        "http://host:bad",
    ],
)
def test_unsafe_endpoints_never_enter_snapshots(url):
    with pytest.raises(ValueError, match="ENDPOINT_INVALID") as error:
        resolve_execution(
            agent="codex",
            provider="ollama",
            model="qwen3-coder",
            allow_unverified=True,
            environ={"AWF_OLLAMA_BASE_URL": url},
        )
    assert "secret" not in str(error.value)


def test_credentials_are_references_and_openrouter_endpoint_is_fixed():
    selected = resolve_execution(
        agent="claude_code",
        provider="openrouter",
        model="anthropic/claude-test",
        allow_unverified=True,
        profile={"runtime": {"environment": {"OPENROUTER_API_KEY": "${ROUTER_TOKEN}"}}},
    )
    assert selected.credential_ref == "env/ROUTER_TOKEN"
    assert selected.endpoint == "https://openrouter.ai/api"
    assert selected.effort is None
    with pytest.raises(ValueError, match="CREDENTIAL_REFERENCE_REQUIRED"):
        resolve_execution(
            agent="codex",
            provider="openrouter",
            model="x",
            allow_unverified=True,
            profile={"runtime": {"environment": {"OPENROUTER_API_KEY": "secret-literal"}}},
        )


def test_corrupt_or_unknown_contract_does_not_fall_back_to_native():
    for snapshot in [None, {}, {"version": 999}]:
        with pytest.raises(ValueError, match="EXECUTION_CONTRACT_UNSUPPORTED"):
            execution_from_policy({"agent_execution": snapshot})
    with pytest.raises(ValueError, match="PROVIDER_UNSUPPORTED"):
        resolve_execution(agent="cursor", provider="ollama", model="x", allow_unverified=True)
    selected = resolve_execution(agent="codex", provider="ollama", model="x", allow_unverified=True)
    assert ExecutionSelection.model_validate(selected.model_dump()) == selected


def test_competing_model_credentials_must_also_be_references():
    with pytest.raises(ValueError, match="CREDENTIAL_REFERENCE_REQUIRED"):
        resolve_execution(
            agent="codex",
            provider="ollama",
            model="coder",
            allow_unverified=True,
            profile={"runtime": {"environment": {"OPENAI_API_KEY": "never-persist-this"}}},
        )


def test_rotating_credential_value_does_not_change_connection_scope():
    first = resolve_execution(
        agent="codex",
        provider="openrouter",
        model="model",
        allow_unverified=True,
        environ={"OPENROUTER_API_KEY": "first-secret"},
    )
    second = resolve_execution(
        agent="codex",
        provider="openrouter",
        model="model",
        allow_unverified=True,
        environ={"OPENROUTER_API_KEY": "rotated-secret"},
    )
    assert first == second
    assert first.connection_scope() == second.connection_scope()
    independent = second.model_copy(update={"credential_ref": "env/SECOND_ACCOUNT"})
    assert independent.connection_scope() != first.connection_scope()


@pytest.mark.parametrize(
    "updates",
    [
        {"agent": "cursor", "provider": "ollama"},
        {"version": True},
        {"endpoint": "http://u:secret@host"},
        {"credential_ref": "env/OTHER"},
    ],
)
def test_corrupt_native_snapshot_cannot_smuggle_expanded_connection(updates):
    native = resolve_execution(agent="codex").model_dump()
    with pytest.raises(ValueError, match="EXECUTION_CONTRACT_UNSUPPORTED"):
        execution_from_policy({"agent_execution": {**native, **updates}})


@pytest.mark.parametrize(
    "agent,model,provider",
    [
        ("opencode", "ollama/coder", "ollama"),
        ("opencode", "openrouter/test/model", "openrouter"),
        ("cursor", None, "cursor"),
        ("antigravity", None, "antigravity"),
        ("grok", None, "xai"),
    ],
)
def test_other_agent_snapshots_keep_native_or_legacy_provider_semantics(agent, model, provider):
    selected = resolve_execution(agent=agent, model=model)
    assert selected.provider == provider
    assert execution_from_policy({"agent_execution": selected.model_dump()}) == selected


@pytest.mark.parametrize("reference", ["ROUTER_TOKEN", "env/ROUTER_TOKEN"])
def test_openrouter_reuses_declared_env_lease_reference_semantics(reference):
    selected = resolve_execution(
        agent="codex",
        provider="openrouter",
        model="model",
        allow_unverified=True,
        profile={
            "secrets": [
                {"kind": "env", "provider": "env", "target": "OPENROUTER_API_KEY", "ref": reference}
            ]
        },
    )
    assert selected.credential_ref == "env/ROUTER_TOKEN"


def test_required_profile_credential_placeholder_remains_a_reference():
    selected = resolve_execution(
        agent="codex",
        provider="openrouter",
        model="model",
        allow_unverified=True,
        profile={
            "runtime": {"environment": {"OPENROUTER_API_KEY": "${ROUTER_TOKEN:?must be declared}"}}
        },
    )
    assert selected.credential_ref == "env/ROUTER_TOKEN"


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.2",
        "127.255.255.254",
        "[0:0:0:0:0:0:0:1]",
        "[::ffff:127.0.0.2]",
        "[0:0:0:0:0:0:0:0]",
    ],
)
def test_selected_endpoint_reuses_host_local_normalization(host):
    selected = resolve_execution(
        agent="codex",
        provider="ollama",
        model="coder",
        allow_unverified=True,
        environ={"OLLAMA_HOST": f"http://{host}:12345/prefix/v1"},
    )
    assert selected.endpoint == "http://host.docker.internal:12345/prefix"
