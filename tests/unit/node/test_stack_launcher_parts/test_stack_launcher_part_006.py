"""Stack-launcher clarification credential-selection regression tests."""

from __future__ import annotations

import pytest

from awf.db.enums import AgentRuntime
from awf.node import stack_launcher as stack_launcher_mod
from awf.node.compose_manager import AuthMount


@pytest.mark.unit
def test_clarification_inputs_exclude_git_mount_selected_by_provider_variable() -> None:
    """A provider environment value cannot reintroduce a Git auth mount."""
    git_auth = AuthMount(
        source="/host/awf/auth/ws_launcher/ssh",
        target="/home/agent/.ssh",
        mode="ro",
    )

    mounts = stack_launcher_mod._clarification_auth_mounts(  # noqa: SLF001
        (git_auth,),
        agent_environment=(("OPENAI_API_KEY", git_auth.target),),
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.codex,
    )

    assert mounts == ()


@pytest.mark.unit
def test_clarification_inputs_exclude_unselected_claude_backend_settings() -> None:
    """Claude backend credentials stay out unless that backend is enabled."""
    environment = stack_launcher_mod._clarification_agent_environment(  # noqa: SLF001
        (
            ("CLAUDE_CODE_USE_BEDROCK", "0"),
            ("AWS_SECRET_ACCESS_KEY", "unselected-backend-secret"),
            ("CLAUDE_CODE_USE_VERTEX", "0"),
            ("ANTHROPIC_VERTEX_PROJECT_ID", "unselected-vertex-project"),
            ("GIT_ASKPASS", "/run/awf/secrets/bb-askpass.sh"),
        ),
        auth_mounts=(),
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.claude_code,
    )

    assert environment == (
        ("CLAUDE_CODE_USE_BEDROCK", "0"),
        ("CLAUDE_CODE_USE_VERTEX", "0"),
    )


@pytest.mark.unit
def test_clarification_inputs_stage_claude_custom_ca() -> None:
    """Claude re-asks retain a declared custom CA trust store."""
    custom_ca = AuthMount(
        source="/host/awf/secret-leases/ws_launcher/provider-ca.pem",
        target="/run/awf/secrets/provider-ca.pem",
        mode="ro",
    )
    environment = (
        ("ANTHROPIC_API_KEY", "anthropic-key"),
        ("NODE_EXTRA_CA_CERTS", custom_ca.target),
    )

    clarification_environment = stack_launcher_mod._clarification_agent_environment(  # noqa: SLF001
        environment,
        auth_mounts=(custom_ca,),
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.claude_code,
    )
    clarification_mounts = stack_launcher_mod._clarification_auth_mounts(  # noqa: SLF001
        (custom_ca,),
        agent_environment=environment,
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.claude_code,
    )

    assert clarification_environment == (
        ("ANTHROPIC_API_KEY", "anthropic-key"),
        ("NODE_EXTRA_CA_CERTS", "/home/agent/.awf/clarification-auth/0"),
    )
    assert clarification_mounts == (
        AuthMount(
            source=custom_ca.source,
            target="/home/agent/.awf/clarification-auth/0",
            mode="ro",
        ),
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("toggle", "backend_settings"),
    [
        (
            "CLAUDE_CODE_USE_BEDROCK",
            (
                ("AWS_ACCESS_KEY_ID", "AKIA_PROFILE_IDENTIFIER"),
                ("AWS_SECRET_ACCESS_KEY", "bedrock-secret"),
                ("AWS_REGION", "us-west-2"),
            ),
        ),
        (
            "CLAUDE_CODE_USE_VERTEX",
            (
                ("ANTHROPIC_VERTEX_PROJECT_ID", "awf-vertex-project"),
                ("CLOUD_ML_REGION", "us-central1"),
            ),
        ),
    ],
)
def test_clarification_inputs_resolve_host_auth_claude_backend_toggle(
    monkeypatch: pytest.MonkeyPatch,
    toggle: str,
    backend_settings: tuple[tuple[str, str], ...],
) -> None:
    """Host-auth Compose placeholders select the enabled Claude backend."""
    monkeypatch.setenv(toggle, "1")
    environment = stack_launcher_mod._clarification_agent_environment(  # noqa: SLF001
        (
            ("ANTHROPIC_API_KEY", "direct-anthropic-token"),
            (toggle, f"${{{toggle}}}"),
            *backend_settings,
        ),
        auth_mounts=(),
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.claude_code,
    )

    assert environment == ((toggle, f"${{{toggle}}}"), *backend_settings)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("toggle", "enabled_value"),
    [
        ("GOOGLE_GENAI_USE_VERTEXAI", "true"),
        ("GOOGLE_GENAI_USE_GCA", "yes"),
    ],
)
def test_clarification_inputs_resolve_host_auth_gemini_google_cloud_toggle(
    monkeypatch: pytest.MonkeyPatch,
    toggle: str,
    enabled_value: str,
) -> None:
    """Host-auth Compose placeholders select Gemini Google Cloud ADC."""
    gcloud_auth = AuthMount(
        source="/host/awf/auth/ws_launcher/gcloud",
        target="/home/agent/.config/gcloud",
        mode="ro",
    )
    gemini_auth = AuthMount(
        source="/host/awf/auth/ws_launcher/gemini",
        target="/home/agent/.gemini",
        mode="rw",
    )
    monkeypatch.setenv(toggle, enabled_value)
    agent_environment = (
        (toggle, f"${{{toggle}}}"),
        ("GOOGLE_CLOUD_PROJECT", "awf-project"),
    )

    assert (
        stack_launcher_mod._clarification_agent_environment(  # noqa: SLF001
            agent_environment,
            auth_mounts=(gcloud_auth, gemini_auth),
            mirror_target="/host/awf/git/mirrors/repo.git",
            agent_runtime=AgentRuntime.gemini,
        )
        == agent_environment
    )
    assert stack_launcher_mod._clarification_auth_mounts(  # noqa: SLF001
        (gcloud_auth, gemini_auth),
        agent_environment=agent_environment,
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.gemini,
    ) == (AuthMount(source=gcloud_auth.source, target=gcloud_auth.target, mode="ro"),)


@pytest.mark.unit
def test_clarification_environment_computes_provider_names_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clarification reuses provider names while staging its selected mounts."""
    calls = 0
    original = stack_launcher_mod._clarification_model_provider_environment_names  # noqa: SLF001

    def _record_provider_names(
        agent_environment: tuple[tuple[str, str], ...],
        *,
        agent_runtime: AgentRuntime,
        agent_model: str | None = None,
    ) -> frozenset[str]:
        nonlocal calls
        calls += 1
        return original(
            agent_environment,
            agent_runtime=agent_runtime,
            agent_model=agent_model,
        )

    monkeypatch.setattr(
        stack_launcher_mod,
        "_clarification_model_provider_environment_names",
        _record_provider_names,
    )

    environment = stack_launcher_mod._clarification_agent_environment(  # noqa: SLF001
        (("OPENAI_API_KEY", "token"),),
        auth_mounts=(),
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.codex,
    )

    assert environment == (("OPENAI_API_KEY", "token"),)
    assert calls == 1
