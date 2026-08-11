"""Clarification credential staging tests split from stack launcher part 008."""

from __future__ import annotations

import pytest

from awf.db.enums import AgentRuntime
from awf.node import stack_launcher as stack_launcher_mod
from awf.node.compose_manager import AuthMount


@pytest.mark.unit
@pytest.mark.parametrize(
    ("credential_name", "credential_file"),
    (
        ("AWS_CONFIG_FILE", "config"),
        ("AWS_SHARED_CREDENTIALS_FILE", "credentials"),
    ),
)
@pytest.mark.parametrize("operator", (":-", "-"))
def test_clarification_stages_defaulted_bedrock_profile_file_from_custom_mount(
    monkeypatch: pytest.MonkeyPatch,
    credential_name: str,
    credential_file: str,
    operator: str,
) -> None:
    """A defaulted Bedrock profile file resolves before staging its mount."""
    credential_directory = AuthMount(
        source="/host/awf/secret-leases/ws_launcher/aws",
        target="/run/awf/secrets",
        mode="ro",
    )
    monkeypatch.delenv(credential_name, raising=False)
    environment = (
        ("CLAUDE_CODE_USE_BEDROCK", "1"),
        ("AWS_PROFILE", "awf-bedrock"),
        (
            credential_name,
            f"${{{credential_name}{operator}{credential_directory.target}/{credential_file}}}",
        ),
    )

    clarification_environment = stack_launcher_mod._clarification_agent_environment(  # noqa: SLF001
        environment,
        auth_mounts=(credential_directory,),
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.claude_code,
    )
    clarification_mounts = stack_launcher_mod._clarification_auth_mounts(  # noqa: SLF001
        (credential_directory,),
        agent_environment=environment,
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.claude_code,
    )

    assert dict(clarification_environment)[credential_name] == (
        f"/home/agent/.awf/clarification-auth/0/{credential_file}"
    )
    assert clarification_mounts == (
        AuthMount(
            source=credential_directory.source,
            target="/home/agent/.awf/clarification-auth/0",
            mode="ro",
        ),
    )
