"""Clarification credential staging tests split from stack launcher part 008."""

from __future__ import annotations

import pytest

from awf.db.enums import AgentRuntime
from awf.node import stack_launcher as stack_launcher_mod
from awf.node.compose_manager import AuthMount


@pytest.mark.unit
def test_clarification_does_not_expand_unavailable_external_account_adc_mount() -> None:
    """An unavailable ADC file cannot select another declared auth mount."""
    adc_target = "/run/awf/secrets/google/adc.json"
    adc_mount = AuthMount(
        source="/host/awf/secret-leases/ws_launcher/missing-adc.json",
        target=adc_target,
        mode="ro",
    )
    subject_token_mount = AuthMount(
        source="/host/awf/secret-leases/ws_launcher/subject-token",
        target="/run/awf/secrets/google/subject-token",
        mode="ro",
    )
    environment = (
        ("GOOGLE_GENAI_USE_VERTEXAI", "1"),
        ("GOOGLE_APPLICATION_CREDENTIALS", adc_target),
    )

    clarification_mounts = stack_launcher_mod._clarification_auth_mounts(  # noqa: SLF001
        (adc_mount, subject_token_mount),
        agent_environment=environment,
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.gemini,
    )

    assert clarification_mounts == (
        AuthMount(
            source=adc_mount.source,
            target="/home/agent/.awf/clarification-auth/0",
            mode="ro",
        ),
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("agent_runtime", "environment", "credential_name"),
    (
        (
            AgentRuntime.claude_code,
            (
                ("CLAUDE_CODE_USE_VERTEX", "1"),
                ("ANTHROPIC_VERTEX_PROJECT_ID", "awf-vertex-project"),
                ("GOOGLE_APPLICATION_CREDENTIALS", "/run/awf/secrets/gcp.json"),
            ),
            "GOOGLE_APPLICATION_CREDENTIALS",
        ),
        (
            AgentRuntime.claude_code,
            (
                ("CLAUDE_CODE_USE_BEDROCK", "1"),
                ("AWS_PROFILE", "awf-bedrock"),
                ("AWS_CONFIG_FILE", "/run/awf/secrets/config"),
            ),
            "AWS_CONFIG_FILE",
        ),
    ),
)
def test_clarification_stages_credential_file_below_declared_directory_mount(
    agent_runtime: AgentRuntime,
    environment: tuple[tuple[str, str], ...],
    credential_name: str,
) -> None:
    """An explicit credential file retains and rewrites its containing mount."""
    credential_directory = AuthMount(
        source="/host/awf/secret-leases/ws_launcher/provider",
        target="/run/awf/secrets",
        mode="ro",
    )
    mirror_target = "/host/awf/git/mirrors/repo.git"

    clarification_environment = stack_launcher_mod._clarification_agent_environment(  # noqa: SLF001
        environment,
        auth_mounts=(credential_directory,),
        mirror_target=mirror_target,
        agent_runtime=agent_runtime,
    )
    clarification_mounts = stack_launcher_mod._clarification_auth_mounts(  # noqa: SLF001
        (credential_directory,),
        agent_environment=environment,
        mirror_target=mirror_target,
        agent_runtime=agent_runtime,
    )

    assert dict(clarification_environment)[credential_name] == (
        "/home/agent/.awf/clarification-auth/0/"
        + environment[-1][1].removeprefix(f"{credential_directory.target}/")
    )
    assert clarification_mounts == (
        AuthMount(
            source=credential_directory.source,
            target="/home/agent/.awf/clarification-auth/0",
            mode="ro",
        ),
    )
