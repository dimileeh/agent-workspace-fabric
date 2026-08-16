"""Clarification credential staging tests split from stack launcher part 004."""

from __future__ import annotations

import pytest

from awf.db.enums import AgentRuntime
from awf.node import stack_launcher as stack_launcher_mod
from awf.node.compose_manager import AuthMount


@pytest.mark.unit
def test_opencode_clarification_uses_selected_provider_credentials_only() -> None:
    """A provider-qualified OpenCode re-ask omits shared provider auth stores."""
    opencode_auth = AuthMount(
        source="/host/awf/auth/ws_launcher/opencode",
        target="/home/agent/.config/opencode",
        mode="rw",
    )
    openai_credentials = AuthMount(
        source="/host/awf/secret-leases/ws_launcher/openai-key",
        target="/run/awf/secrets/openai-key",
        mode="ro",
    )
    ollama_auth = AuthMount(
        source="/host/awf/auth/ws_launcher/ollama",
        target="/home/agent/.ollama",
        mode="rw",
    )
    anthropic_credentials = AuthMount(
        source="/host/awf/secret-leases/ws_launcher/anthropic-key",
        target="/run/awf/secrets/anthropic-key",
        mode="ro",
    )
    gemini_credentials = AuthMount(
        source="/host/awf/secret-leases/ws_launcher/gemini-key",
        target="/run/awf/secrets/gemini-key",
        mode="ro",
    )
    xai_credentials = AuthMount(
        source="/host/awf/secret-leases/ws_launcher/xai-key",
        target="/run/awf/secrets/xai-key",
        mode="ro",
    )
    environment = (
        ("OPENAI_API_KEY", openai_credentials.target),
        ("OPENAI_BASE_URL", "http://openai-sidecar:4000/v1"),
        ("ANTHROPIC_API_KEY", anthropic_credentials.target),
        ("ANTHROPIC_BASE_URL", "http://anthropic-sidecar:4001"),
        ("GEMINI_API_KEY", gemini_credentials.target),
        ("XAI_API_KEY", xai_credentials.target),
    )
    mounts = (
        opencode_auth,
        ollama_auth,
        openai_credentials,
        anthropic_credentials,
        gemini_credentials,
        xai_credentials,
    )

    clarification_environment = stack_launcher_mod._clarification_agent_environment(  # noqa: SLF001
        environment,
        auth_mounts=mounts,
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.opencode,
        agent_model="openai/gpt-5",
    )
    clarification_mounts = stack_launcher_mod._clarification_auth_mounts(  # noqa: SLF001
        mounts,
        agent_environment=environment,
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.opencode,
        agent_model="openai/gpt-5",
    )

    assert clarification_environment == (
        ("OPENAI_API_KEY", "/home/agent/.awf/clarification-auth/0"),
        ("OPENAI_BASE_URL", "http://openai-sidecar:4000/v1"),
    )
    assert clarification_mounts == (
        AuthMount(
            source=openai_credentials.source,
            target="/home/agent/.awf/clarification-auth/0",
            mode="ro",
        ),
    )
