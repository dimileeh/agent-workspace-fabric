"""Managed OpenRouter keys keep one fixed, secret-free credential identity."""

import pytest

from awf.common.agent_execution import (
    EXECUTION_POLICY_KEY,
    MANAGED_OPENROUTER_CREDENTIAL_REF,
    ExecutionSelection,
    bind_execution_profile,
    circuit_key,
    execution_from_policy,
    managed_openrouter_frozen,
    resolve_execution,
)


def _managed(agent: str = "codex", model: str = "moonshotai/kimi-k2.7-code", **kwargs):
    return resolve_execution(
        agent=agent,
        provider="openrouter",
        model=model,
        allow_unverified=True,
        managed_openrouter=True,
        **kwargs,
    )


@pytest.mark.parametrize("agent", ["codex", "claude_code"])
def test_managed_openrouter_uses_the_fixed_credential_reference(agent):
    selected = _managed(agent)

    assert selected.credential_ref == MANAGED_OPENROUTER_CREDENTIAL_REF == "managed/openrouter"
    assert selected.managed_key is True
    assert selected.endpoint == "https://openrouter.ai/api"
    # A profile alias or declared lease never replaces the managed identity.
    aliased = _managed(
        agent,
        profile={"runtime": {"environment": {"OPENROUTER_API_KEY": "${TEAM_OPENROUTER}"}}},
    )
    assert aliased.credential_ref == MANAGED_OPENROUTER_CREDENTIAL_REF


def test_managed_resolution_still_rejects_inline_credentials():
    with pytest.raises(ValueError, match="PROVIDER_CREDENTIAL_REFERENCE_REQUIRED"):
        _managed(profile={"runtime": {"environment": {"OPENROUTER_API_KEY": "sk-or-v1-inline"}}})


def test_policy_round_trips_only_the_exact_managed_reference():
    selected = _managed()
    policy = {EXECUTION_POLICY_KEY: selected.model_dump(mode="json")}

    assert execution_from_policy(policy) == selected
    static = resolve_execution(
        agent="codex", provider="openrouter", model="m/x", allow_unverified=True
    )
    assert static.managed_key is False
    for bad in (
        {**selected.model_dump(mode="json"), "credential_ref": "managed/other"},
        {**selected.model_dump(mode="json"), "credential_ref": "managed/openrouter/x"},
        {
            **resolve_execution(
                agent="codex", provider="ollama", model="qwen3-coder", allow_unverified=True
            ).model_dump(mode="json"),
            "credential_ref": MANAGED_OPENROUTER_CREDENTIAL_REF,
        },
        {
            **resolve_execution(agent="codex").model_dump(mode="json"),
            "credential_ref": MANAGED_OPENROUTER_CREDENTIAL_REF,
        },
    ):
        with pytest.raises(ValueError, match="EXECUTION_CONTRACT_UNSUPPORTED"):
            execution_from_policy({EXECUTION_POLICY_KEY: bad})


def test_managed_connection_scope_is_stable_secret_free_and_not_per_workspace():
    first = _managed()
    second = _managed()
    static = resolve_execution(
        agent="codex", provider="openrouter", model=first.model, allow_unverified=True
    )

    scope = first.connection_scope("ws_" + "a" * 24)
    assert scope == second.connection_scope("ws_" + "b" * 24) == first.connection_scope()
    assert scope != static.connection_scope()
    assert "sk-or" not in scope
    ordinary = {
        EXECUTION_POLICY_KEY: first.model_dump(mode="json"),
        "provider_budget": {"limit_usd": 10},
    }
    diagnostic = {
        EXECUTION_POLICY_KEY: second.model_dump(mode="json"),
        "provider_budget": {"limit_usd": 1},
    }
    assert circuit_key("codex", ordinary, workspace_id="ws_1") == circuit_key(
        "codex", diagnostic, workspace_id="ws_2"
    )
    # The budget is a sibling of the selection, so exact evidence matching holds.
    assert execution_from_policy(ordinary) == execution_from_policy(diagnostic)


@pytest.mark.parametrize(
    ("agent", "provider", "model"),
    [
        ("codex", "ollama", "qwen3-coder"),
        ("claude_code", "ollama", "qwen3-coder"),
        ("codex", None, None),
        ("claude_code", None, None),
        ("opencode", None, "openrouter/moonshotai/kimi-k2"),
    ],
)
def test_managed_flag_does_not_change_other_selections(agent, provider, model):
    kwargs = {"agent": agent, "provider": provider, "model": model}
    if provider is not None:
        kwargs["allow_unverified"] = True
    assert resolve_execution(**kwargs, managed_openrouter=True) == resolve_execution(**kwargs)


def test_bind_execution_profile_honours_the_frozen_managed_flag():
    pending = {
        "agent_execution_profile_pending": True,
        "agent_provider": "openrouter",
        "agent_model": "m/x",
        "allow_unverified_model": True,
        "provider_budget": {"managed": True, "limit_usd": 10.0},
        "provider_recovery": {
            "fallbacks": [
                {"agent": "claude_code", "provider": "openrouter", "model": "m/y"},
                {"agent": "codex", "provider": "ollama", "model": "qwen3-coder"},
            ]
        },
    }
    bound = bind_execution_profile(pending, agent="codex", profile={}, environ={})

    primary = execution_from_policy(bound)
    fallbacks = [
        execution_from_policy(target) for target in bound["provider_recovery"]["fallbacks"]
    ]
    assert primary is not None and primary.managed_key
    assert fallbacks[0] is not None and fallbacks[0].managed_key
    assert fallbacks[1] is not None and fallbacks[1].credential_ref is None
    static = bind_execution_profile(
        {**pending, "provider_budget": {"managed": False}}, agent="codex", profile={}, environ={}
    )
    assert execution_from_policy(static).credential_ref == "env/OPENROUTER_API_KEY"


def test_managed_openrouter_frozen_reads_only_the_policy_flag():
    assert managed_openrouter_frozen({"provider_budget": {"managed": True}}) is True
    assert managed_openrouter_frozen({"provider_budget": {"managed": False}}) is False
    assert managed_openrouter_frozen({"provider_budget": "corrupt"}) is False
    assert managed_openrouter_frozen({}) is False
    assert managed_openrouter_frozen(None) is False


def test_selection_model_has_no_budget_field():
    assert "provider_budget" not in ExecutionSelection.model_fields
