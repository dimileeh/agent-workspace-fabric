"""Stack launcher OpenCode clarification tests."""

from __future__ import annotations

import pytest

from awf.db.enums import AgentRuntime
from awf.node import stack_launcher as stack_launcher_mod
from awf.node.compose_manager import AuthMount


@pytest.mark.unit
def test_opencode_clarification_retains_selected_anthropic_provider_base_url() -> None:
    """An Anthropic OpenCode re-ask keeps its endpoint but not OpenAI's."""
    environment = (
        ("OPENAI_API_KEY", "openai-key"),
        ("OPENAI_BASE_URL", "http://openai-sidecar:4000/v1"),
        ("ANTHROPIC_API_KEY", "anthropic-key"),
        ("ANTHROPIC_BASE_URL", "http://anthropic-sidecar:4001"),
    )

    clarification_environment = stack_launcher_mod._clarification_agent_environment(  # noqa: SLF001
        environment,
        auth_mounts=(),
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.opencode,
        agent_model="anthropic/claude-sonnet",
    )

    assert clarification_environment == (
        ("ANTHROPIC_API_KEY", "anthropic-key"),
        ("ANTHROPIC_BASE_URL", "http://anthropic-sidecar:4001"),
    )


@pytest.mark.unit
def test_opencode_clarification_stages_config_auth_without_provider_environment() -> None:
    """A provider-qualified re-ask retains OpenCode file auth as a fallback."""
    opencode_auth = AuthMount(
        source="/host/awf/auth/ws_launcher/opencode",
        target="/home/agent/.config/opencode",
        mode="rw",
    )

    clarification_mounts = stack_launcher_mod._clarification_auth_mounts(  # noqa: SLF001
        (opencode_auth,),
        agent_environment=(),
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.opencode,
        agent_model="openai/gpt-5",
    )

    assert clarification_mounts == (
        AuthMount(source=opencode_auth.source, target=opencode_auth.target, mode="ro"),
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("agent_environment", "agent_model"),
    [
        (("OPENAI_BASE_URL", "http://openai-sidecar:4000/v1"), "openai/gpt-5"),
        (("ANTHROPIC_BASE_URL", "http://anthropic-sidecar:4001"), "anthropic/claude-sonnet"),
    ],
)
def test_opencode_clarification_stages_config_auth_with_only_a_provider_base_url(
    agent_environment: tuple[str, str], agent_model: str
) -> None:
    """A provider endpoint alone does not replace OpenCode file authentication."""
    opencode_auth = AuthMount(
        source="/host/awf/auth/ws_launcher/opencode",
        target="/home/agent/.config/opencode",
        mode="rw",
    )

    clarification_mounts = stack_launcher_mod._clarification_auth_mounts(  # noqa: SLF001
        (opencode_auth,),
        agent_environment=(agent_environment,),
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.opencode,
        agent_model=agent_model,
    )

    assert clarification_mounts == (
        AuthMount(source=opencode_auth.source, target=opencode_auth.target, mode="ro"),
    )


@pytest.mark.unit
def test_opencode_clarification_stages_config_auth_for_unset_optional_provider_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unset optional Compose key does not replace OpenCode file auth."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    opencode_auth = AuthMount(
        source="/host/awf/auth/ws_launcher/opencode",
        target="/home/agent/.config/opencode",
        mode="rw",
    )

    clarification_mounts = stack_launcher_mod._clarification_auth_mounts(  # noqa: SLF001
        (opencode_auth,),
        agent_environment=(("OPENAI_API_KEY", "${OPENAI_API_KEY:-}"),),
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.opencode,
        agent_model="openai/gpt-5",
    )

    assert clarification_mounts == (
        AuthMount(source=opencode_auth.source, target=opencode_auth.target, mode="ro"),
    )


@pytest.mark.unit
def test_opencode_ollama_clarification_omits_shared_opencode_store() -> None:
    """Ollama re-asks retain Ollama auth without mounting multi-provider config."""
    opencode_auth = AuthMount(
        source="/host/awf/auth/ws_launcher/opencode",
        target="/home/agent/.config/opencode",
        mode="rw",
    )
    ollama_auth = AuthMount(
        source="/host/awf/auth/ws_launcher/ollama",
        target="/home/agent/.ollama",
        mode="rw",
    )
    ollama_credentials = AuthMount(
        source="/host/awf/secret-leases/ws_launcher/ollama-key",
        target="/run/awf/secrets/ollama-key",
        mode="ro",
    )
    environment = (("OLLAMA_API_KEY", ollama_credentials.target),)
    mounts = (opencode_auth, ollama_auth, ollama_credentials)

    clarification_environment = stack_launcher_mod._clarification_agent_environment(  # noqa: SLF001
        environment,
        auth_mounts=mounts,
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.opencode,
        agent_model="ollama/kimi-k2.6:cloud",
    )
    clarification_mounts = stack_launcher_mod._clarification_auth_mounts(  # noqa: SLF001
        mounts,
        agent_environment=environment,
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.opencode,
        agent_model="ollama/kimi-k2.6:cloud",
    )

    assert clarification_environment == (
        ("OLLAMA_API_KEY", "/home/agent/.awf/clarification-auth/1"),
    )
    assert clarification_mounts == (
        AuthMount(source=ollama_auth.source, target=ollama_auth.target, mode="ro"),
        AuthMount(
            source=ollama_credentials.source,
            target="/home/agent/.awf/clarification-auth/1",
            mode="ro",
        ),
    )
