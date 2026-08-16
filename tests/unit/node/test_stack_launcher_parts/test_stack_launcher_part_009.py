"""Clarification credential staging tests split from stack launcher part 008."""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.db.enums import AgentRuntime
from awf.node import stack_launcher as stack_launcher_mod
from awf.node.compose_manager import AuthMount
from awf.node.stack_launcher_auth_helpers import aws_profile_path_rewrites


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


@pytest.mark.unit
def test_clarification_stages_chained_bedrock_profile_auth_mounts(tmp_path: Path) -> None:
    """Bedrock profile chains retain source-profile helper and token mounts."""
    aws_home = tmp_path / "aws"
    aws_home.mkdir()
    helper = tmp_path / "aws-helper"
    token = tmp_path / "web-identity-token"
    helper.write_text("#!/bin/sh\n", encoding="utf-8")
    token.write_text("token", encoding="utf-8")
    helper_target = "/run/awf/secrets/aws-helper"
    token_target = "/run/awf/secrets/web-identity-token"
    (aws_home / "config").write_text(
        "[profile awf-bedrock]\n"
        "source_profile = awf-source\n"
        "[profile awf-source]\n"
        f"credential_process = {helper_target} --json\n"
        f"web_identity_token_file = {token_target}\n",
        encoding="utf-8",
    )
    aws_profile = AuthMount(source=str(aws_home), target="/home/agent/.aws", mode="ro")
    helper_mount = AuthMount(source=str(helper), target=helper_target, mode="ro")
    token_mount = AuthMount(source=str(token), target=token_target, mode="ro")
    environment = (
        ("CLAUDE_CODE_USE_BEDROCK", "1"),
        ("AWS_PROFILE", "awf-bedrock"),
    )
    mounts = (aws_profile, helper_mount, token_mount)

    assert stack_launcher_mod._clarification_auth_mounts(  # noqa: SLF001
        mounts,
        agent_environment=environment,
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.claude_code,
    ) == (
        AuthMount(source=str(aws_home), target="/home/agent/.aws", mode="ro"),
        AuthMount(
            source=str(helper),
            target="/home/agent/.awf/clarification-auth/1",
            mode="ro",
        ),
        AuthMount(
            source=str(token),
            target="/home/agent/.awf/clarification-auth/2",
            mode="ro",
        ),
    )
    assert aws_profile_path_rewrites(
        mounts,
        agent_environment=environment,
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.claude_code,
    ) == (
        (helper_target, "/home/agent/.awf/clarification-auth/1"),
        (token_target, "/home/agent/.awf/clarification-auth/2"),
    )
