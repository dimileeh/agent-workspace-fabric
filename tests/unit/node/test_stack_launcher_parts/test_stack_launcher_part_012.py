"""Clarification credential staging tests split from stack launcher part 004."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from awf.db.enums import AgentRuntime
from awf.node import stack_launcher as stack_launcher_mod
from awf.node.compose_manager import AuthMount
from awf.node.stack_launcher_auth_helpers import (
    aws_profile_path_rewrites,
)


@pytest.mark.unit
def test_clarification_stages_web_identity_when_credential_process_is_unparseable(
    tmp_path: Path,
) -> None:
    """A bad credential process does not hide a valid web-identity token."""
    aws_home = tmp_path / "aws"
    aws_home.mkdir()
    token = tmp_path / "web-identity-token"
    token.write_text("token", encoding="utf-8")
    token_target = "/run/awf/secrets/web-identity-token"
    (aws_home / "config").write_text(
        "[profile awf-bedrock]\n"
        'credential_process = "unclosed\n'
        f"web_identity_token_file = {token_target}\n",
        encoding="utf-8",
    )
    aws_profile = AuthMount(source=str(aws_home), target="/home/agent/.aws", mode="ro")
    token_mount = AuthMount(source=str(token), target=token_target, mode="ro")
    environment = (
        ("CLAUDE_CODE_USE_BEDROCK", "1"),
        ("AWS_PROFILE", "awf-bedrock"),
    )
    mounts = (aws_profile, token_mount)

    assert stack_launcher_mod._clarification_auth_mounts(  # noqa: SLF001
        mounts,
        agent_environment=environment,
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.claude_code,
    ) == (
        AuthMount(source=str(aws_home), target="/home/agent/.aws", mode="ro"),
        AuthMount(
            source=str(token),
            target="/home/agent/.awf/clarification-auth/1",
            mode="ro",
        ),
    )
    assert aws_profile_path_rewrites(
        mounts,
        agent_environment=environment,
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.claude_code,
    ) == ((token_target, "/home/agent/.awf/clarification-auth/1"),)


@pytest.mark.unit
@pytest.mark.parametrize(
    "adc_configuration",
    (
        "not-json",
        {
            "type": "service_account",
            "credential_source": {"file": "/run/awf/secrets/google/subject-token"},
        },
        {
            "type": "external_account",
            "credential_source": {"file": "relative-subject-token"},
        },
        {
            "type": "external_account",
            "credential_source": {"file": "/run/awf/secrets/google/not-declared"},
        },
    ),
)
def test_clarification_does_not_expand_invalid_external_account_adc_mounts(
    tmp_path: Path,
    adc_configuration: str | dict[str, object],
) -> None:
    """Only valid external-account configs may select a second auth mount."""
    adc_config = tmp_path / "adc.json"
    adc_config.write_text(
        adc_configuration if isinstance(adc_configuration, str) else json.dumps(adc_configuration),
        encoding="utf-8",
    )
    adc_target = "/run/awf/secrets/google/adc.json"
    adc_mount = AuthMount(source=str(adc_config), target=adc_target, mode="ro")
    subject_token_mount = AuthMount(
        source=str(tmp_path / "subject-token"),
        target="/run/awf/secrets/google/subject-token",
        mode="ro",
    )
    environment = (
        ("CLAUDE_CODE_USE_VERTEX", "1"),
        ("ANTHROPIC_VERTEX_PROJECT_ID", "awf-vertex-project"),
        ("GOOGLE_APPLICATION_CREDENTIALS", adc_target),
    )

    clarification_mounts = stack_launcher_mod._clarification_auth_mounts(  # noqa: SLF001
        (adc_mount, subject_token_mount),
        agent_environment=environment,
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.claude_code,
    )

    assert clarification_mounts == (
        AuthMount(
            source=str(adc_config),
            target="/home/agent/.awf/clarification-auth/0",
            mode="ro",
        ),
    )
