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
    external_account_subject_token_file_rewrites,
)


@pytest.mark.unit
def test_clarification_inputs_prefer_static_bedrock_credentials_to_profile_and_web_identity() -> (
    None
):
    """Bedrock clarification does not stage inactive AWS credential files."""
    aws_shared_credentials = AuthMount(
        source="/host/awf/secret-leases/ws_launcher/aws-credentials",
        target="/run/awf/secrets/aws-credentials",
        mode="ro",
    )
    aws_config = AuthMount(
        source="/host/awf/secret-leases/ws_launcher/aws-config",
        target="/run/awf/secrets/aws-config",
        mode="ro",
    )
    aws_web_identity_token = AuthMount(
        source="/host/awf/secret-leases/ws_launcher/aws-web-identity-token",
        target="/run/awf/secrets/aws-web-identity-token",
        mode="ro",
    )
    environment = (
        ("CLAUDE_CODE_USE_BEDROCK", "1"),
        ("AWS_ACCESS_KEY_ID", "AKIA_PROFILE_IDENTIFIER"),
        ("AWS_SECRET_ACCESS_KEY", "static-secret"),
        ("AWS_SESSION_TOKEN", "static-session-token"),
        ("AWS_REGION", "us-west-2"),
        ("AWS_DEFAULT_REGION", "us-east-1"),
        ("AWS_PROFILE", "awf-bedrock"),
        ("AWS_SHARED_CREDENTIALS_FILE", aws_shared_credentials.target),
        ("AWS_CONFIG_FILE", aws_config.target),
        ("AWS_ROLE_ARN", "arn:aws:iam::123456789012:role/awf-bedrock"),
        ("AWS_WEB_IDENTITY_TOKEN_FILE", aws_web_identity_token.target),
    )
    mounts = (aws_shared_credentials, aws_config, aws_web_identity_token)

    clarification_environment = stack_launcher_mod._clarification_agent_environment(  # noqa: SLF001
        environment,
        auth_mounts=mounts,
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.claude_code,
    )
    clarification_mounts = stack_launcher_mod._clarification_auth_mounts(  # noqa: SLF001
        mounts,
        agent_environment=environment,
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.claude_code,
    )

    assert clarification_environment == (
        ("CLAUDE_CODE_USE_BEDROCK", "1"),
        ("AWS_ACCESS_KEY_ID", "AKIA_PROFILE_IDENTIFIER"),
        ("AWS_SECRET_ACCESS_KEY", "static-secret"),
        ("AWS_SESSION_TOKEN", "static-session-token"),
        ("AWS_REGION", "us-west-2"),
        ("AWS_DEFAULT_REGION", "us-east-1"),
    )
    assert clarification_mounts == ()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("environment_values", "expected_names"),
    [
        (
            {
                "AWS_BEARER_TOKEN_BEDROCK": "bedrock-api-key",
                "AWS_ACCESS_KEY_ID": "AKIA_PROFILE_IDENTIFIER",
                "AWS_SECRET_ACCESS_KEY": "static-secret",
            },
            frozenset(
                {
                    "AWS_BEARER_TOKEN_BEDROCK",
                    "AWS_REGION",
                    "AWS_DEFAULT_REGION",
                }
            ),
        ),
        (
            {
                "AWS_BEARER_TOKEN_BEDROCK": "${AWS_BEARER_TOKEN_BEDROCK:-}",
                "AWS_PROFILE": "awf-bedrock",
            },
            frozenset(
                {
                    "AWS_REGION",
                    "AWS_DEFAULT_REGION",
                    "AWS_PROFILE",
                    "AWS_SHARED_CREDENTIALS_FILE",
                    "AWS_CONFIG_FILE",
                }
            ),
        ),
        (
            {
                "AWS_PROFILE": "awf-bedrock",
                "AWS_CONFIG_FILE": "/run/awf/secrets/aws-config",
                "AWS_ROLE_ARN": "arn:aws:iam::123456789012:role/awf-bedrock",
                "AWS_WEB_IDENTITY_TOKEN_FILE": "/run/awf/secrets/aws-web-identity-token",
            },
            frozenset(
                {
                    "AWS_REGION",
                    "AWS_DEFAULT_REGION",
                    "AWS_ROLE_ARN",
                    "AWS_WEB_IDENTITY_TOKEN_FILE",
                }
            ),
        ),
        (
            {
                "AWS_ROLE_ARN": "arn:aws:iam::123456789012:role/awf-bedrock",
                "AWS_WEB_IDENTITY_TOKEN_FILE": "/run/awf/secrets/aws-web-identity-token",
            },
            frozenset(
                {
                    "AWS_REGION",
                    "AWS_DEFAULT_REGION",
                    "AWS_ROLE_ARN",
                    "AWS_WEB_IDENTITY_TOKEN_FILE",
                }
            ),
        ),
        (
            {"AWS_REGION": "us-west-2"},
            frozenset({"AWS_REGION", "AWS_DEFAULT_REGION"}),
        ),
    ],
)
def test_clarification_bedrock_environment_selects_one_usable_credential_source(
    monkeypatch: pytest.MonkeyPatch,
    environment_values: dict[str, str],
    expected_names: frozenset[str],
) -> None:
    """Bedrock clarification retains a single usable credential source."""
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    assert (
        stack_launcher_mod._clarification_claude_code_bedrock_environment_names(  # noqa: SLF001
            environment_values
        )
        == expected_names
    )


@pytest.mark.unit
def test_clarification_inputs_retain_vertex_adc_directory() -> None:
    """Vertex clarification retains ADC without exposing Claude file auth."""
    claude_auth = AuthMount(
        source="/host/awf/auth/ws_launcher/claude",
        target="/home/agent/.claude",
        mode="rw",
    )
    gcloud_auth = AuthMount(
        source="/host/awf/auth/ws_launcher/gcloud",
        target="/home/agent/.config/gcloud",
        mode="ro",
    )
    environment = (
        ("CLAUDE_CODE_USE_VERTEX", "1"),
        ("ANTHROPIC_VERTEX_PROJECT_ID", "awf-vertex-project"),
        ("CLOUD_ML_REGION", "us-central1"),
    )

    clarification_mounts = stack_launcher_mod._clarification_auth_mounts(  # noqa: SLF001
        (claude_auth, gcloud_auth),
        agent_environment=environment,
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.claude_code,
    )

    assert clarification_mounts == (
        AuthMount(source=gcloud_auth.source, target=gcloud_auth.target, mode="ro"),
    )


@pytest.mark.unit
def test_clarification_inputs_retain_bedrock_profile_directory() -> None:
    """Bedrock profile clarification retains its standard AWS credential directory."""
    claude_auth = AuthMount(
        source="/host/awf/auth/ws_launcher/claude",
        target="/home/agent/.claude",
        mode="rw",
    )
    aws_profile_directory = AuthMount(
        source="/host/awf/auth/ws_launcher/aws",
        target="/home/agent/.aws",
        mode="rw",
    )
    environment = (
        ("CLAUDE_CODE_USE_BEDROCK", "1"),
        ("AWS_PROFILE", "awf-bedrock"),
        ("AWS_REGION", "us-west-2"),
    )

    clarification_environment = stack_launcher_mod._clarification_agent_environment(  # noqa: SLF001
        environment,
        auth_mounts=(claude_auth, aws_profile_directory),
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.claude_code,
    )
    clarification_mounts = stack_launcher_mod._clarification_auth_mounts(  # noqa: SLF001
        (claude_auth, aws_profile_directory),
        agent_environment=environment,
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.claude_code,
    )

    assert clarification_environment == environment
    assert clarification_mounts == (
        AuthMount(
            source=aws_profile_directory.source,
            target=aws_profile_directory.target,
            mode="ro",
        ),
    )


@pytest.mark.unit
def test_clarification_inputs_retain_bedrock_default_profile_directory() -> None:
    """Bedrock clarification retains the implicit AWS default-profile directory."""
    aws_profile_directory = AuthMount(
        source="/host/awf/auth/ws_launcher/aws",
        target="/home/agent/.aws",
        mode="rw",
    )
    environment = (
        ("CLAUDE_CODE_USE_BEDROCK", "1"),
        ("AWS_REGION", "us-west-2"),
    )

    clarification_mounts = stack_launcher_mod._clarification_auth_mounts(  # noqa: SLF001
        (aws_profile_directory,),
        agent_environment=environment,
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.claude_code,
    )

    assert clarification_mounts == (
        AuthMount(
            source=aws_profile_directory.source,
            target=aws_profile_directory.target,
            mode="ro",
        ),
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("environment_name", "environment_value"),
    (
        ("AWS_CONFIG_FILE", "/home/agent/.aws/config"),
        ("AWS_SHARED_CREDENTIALS_FILE", "/home/agent/.aws/credentials"),
    ),
)
def test_clarification_inputs_retain_default_bedrock_profile_directory_for_file_auth(
    environment_name: str,
    environment_value: str,
) -> None:
    """Default-profile file auth within the AWS directory remains available."""
    aws_profile_directory = AuthMount(
        source="/host/awf/auth/ws_launcher/aws",
        target="/home/agent/.aws",
        mode="rw",
    )
    environment = (
        ("CLAUDE_CODE_USE_BEDROCK", "1"),
        (environment_name, environment_value),
        ("AWS_REGION", "us-west-2"),
    )

    clarification_mounts = stack_launcher_mod._clarification_auth_mounts(  # noqa: SLF001
        (aws_profile_directory,),
        agent_environment=environment,
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.claude_code,
    )

    assert clarification_mounts == (
        AuthMount(
            source=aws_profile_directory.source,
            target=aws_profile_directory.target,
            mode="ro",
        ),
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("environment_name", "default_path"),
    (
        ("AWS_CONFIG_FILE", "/home/agent/.aws/config"),
        ("AWS_SHARED_CREDENTIALS_FILE", "/home/agent/.aws/credentials"),
    ),
)
@pytest.mark.parametrize("operator", (":-", "-"))
def test_clarification_inputs_retain_bedrock_profile_directory_for_defaulted_file_auth(
    monkeypatch: pytest.MonkeyPatch,
    environment_name: str,
    default_path: str,
    operator: str,
) -> None:
    """Defaulted Compose file paths select the standard AWS credential directory."""
    aws_profile_directory = AuthMount(
        source="/host/awf/auth/ws_launcher/aws",
        target="/home/agent/.aws",
        mode="rw",
    )
    monkeypatch.delenv(environment_name, raising=False)
    environment = (
        ("CLAUDE_CODE_USE_BEDROCK", "1"),
        ("AWS_PROFILE", "awf-bedrock"),
        (environment_name, f"${{{environment_name}{operator}{default_path}}}"),
        ("AWS_REGION", "us-west-2"),
    )

    clarification_mounts = stack_launcher_mod._clarification_auth_mounts(  # noqa: SLF001
        (aws_profile_directory,),
        agent_environment=environment,
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.claude_code,
    )

    assert clarification_mounts == (
        AuthMount(
            source=aws_profile_directory.source,
            target=aws_profile_directory.target,
            mode="ro",
        ),
    )


@pytest.mark.unit
def test_clarification_inputs_retain_bedrock_profile_directory_for_its_config_file() -> None:
    """Bedrock profile config within its standard directory remains available."""
    aws_profile_directory = AuthMount(
        source="/host/awf/auth/ws_launcher/aws",
        target="/home/agent/.aws",
        mode="rw",
    )
    environment = (
        ("CLAUDE_CODE_USE_BEDROCK", "1"),
        ("AWS_PROFILE", "awf-bedrock"),
        ("AWS_CONFIG_FILE", "/home/agent/.aws/config"),
        ("AWS_REGION", "us-west-2"),
    )

    clarification_environment = stack_launcher_mod._clarification_agent_environment(  # noqa: SLF001
        environment,
        auth_mounts=(aws_profile_directory,),
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.claude_code,
    )
    clarification_mounts = stack_launcher_mod._clarification_auth_mounts(  # noqa: SLF001
        (aws_profile_directory,),
        agent_environment=environment,
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.claude_code,
    )

    assert clarification_environment == (
        ("CLAUDE_CODE_USE_BEDROCK", "1"),
        ("AWS_PROFILE", "awf-bedrock"),
        ("AWS_CONFIG_FILE", "/home/agent/.aws/config"),
        ("AWS_REGION", "us-west-2"),
    )
    assert clarification_mounts == (
        AuthMount(
            source=aws_profile_directory.source,
            target=aws_profile_directory.target,
            mode="ro",
        ),
    )


@pytest.mark.unit
def test_clarification_inputs_retain_bedrock_profile_directory_for_shared_credentials_file() -> (
    None
):
    """Bedrock profile credentials within its standard directory remain available."""
    aws_profile_directory = AuthMount(
        source="/host/awf/auth/ws_launcher/aws",
        target="/home/agent/.aws",
        mode="rw",
    )
    environment = (
        ("CLAUDE_CODE_USE_BEDROCK", "1"),
        ("AWS_PROFILE", "awf-bedrock"),
        ("AWS_SHARED_CREDENTIALS_FILE", "/home/agent/.aws/credentials"),
        ("AWS_REGION", "us-west-2"),
    )

    clarification_mounts = stack_launcher_mod._clarification_auth_mounts(  # noqa: SLF001
        (aws_profile_directory,),
        agent_environment=environment,
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.claude_code,
    )

    assert clarification_mounts == (
        AuthMount(
            source=aws_profile_directory.source,
            target=aws_profile_directory.target,
            mode="ro",
        ),
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    (
        "external_name",
        "external_target",
    ),
    (
        (
            "AWS_CONFIG_FILE",
            "/run/awf/secrets/aws-config",
        ),
        (
            "AWS_SHARED_CREDENTIALS_FILE",
            "/run/awf/secrets/aws-credentials",
        ),
    ),
)
def test_clarification_bedrock_profile_preserves_complementary_default_file(
    external_name: str,
    external_target: str,
) -> None:
    """An external profile file retains its complementary default AWS file."""
    aws_profile_directory = AuthMount(
        source="/host/awf/auth/ws_launcher/aws",
        target="/home/agent/.aws",
        mode="rw",
    )
    external_profile_file = AuthMount(
        source=f"/host/awf/secret-leases/ws_launcher/{external_name.lower()}",
        target=external_target,
        mode="ro",
    )
    environment = (
        ("CLAUDE_CODE_USE_BEDROCK", "1"),
        ("AWS_PROFILE", "awf-bedrock"),
        (external_name, external_target),
    )

    clarification_mounts = stack_launcher_mod._clarification_auth_mounts(  # noqa: SLF001
        (aws_profile_directory, external_profile_file),
        agent_environment=environment,
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.claude_code,
    )

    assert clarification_mounts == (
        AuthMount(
            source=aws_profile_directory.source,
            target=aws_profile_directory.target,
            mode="ro",
        ),
        AuthMount(
            source=external_profile_file.source,
            target="/home/agent/.awf/clarification-auth/1",
            mode="ro",
        ),
    )


@pytest.mark.unit
def test_clarification_bedrock_profile_excludes_directory_when_both_files_are_external() -> None:
    """Fully explicit profile files do not retain the inactive default directory."""
    aws_profile_directory = AuthMount(
        source="/host/awf/auth/ws_launcher/aws",
        target="/home/agent/.aws",
        mode="rw",
    )
    aws_config_file = AuthMount(
        source="/host/awf/secret-leases/ws_launcher/aws-config",
        target="/run/awf/secrets/aws-config",
        mode="ro",
    )
    aws_credentials_file = AuthMount(
        source="/host/awf/secret-leases/ws_launcher/aws-credentials",
        target="/run/awf/secrets/aws-credentials",
        mode="ro",
    )
    environment = (
        ("CLAUDE_CODE_USE_BEDROCK", "1"),
        ("AWS_PROFILE", "awf-bedrock"),
        ("AWS_CONFIG_FILE", aws_config_file.target),
        ("AWS_SHARED_CREDENTIALS_FILE", aws_credentials_file.target),
    )

    clarification_mounts = stack_launcher_mod._clarification_auth_mounts(  # noqa: SLF001
        (aws_profile_directory, aws_config_file, aws_credentials_file),
        agent_environment=environment,
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.claude_code,
    )

    assert clarification_mounts == (
        AuthMount(
            source=aws_config_file.source,
            target="/home/agent/.awf/clarification-auth/0",
            mode="ro",
        ),
        AuthMount(
            source=aws_credentials_file.source,
            target="/home/agent/.awf/clarification-auth/1",
            mode="ro",
        ),
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("environment_name", "profile_file_name"),
    (
        ("AWS_CONFIG_FILE", "aws-config"),
        ("AWS_SHARED_CREDENTIALS_FILE", "aws-credentials"),
    ),
)
def test_clarification_bedrock_profile_directory_excludes_traversal_profile_file(
    environment_name: str,
    profile_file_name: str,
) -> None:
    """A traversal profile path does not select the standard AWS directory."""
    aws_profile_directory = AuthMount(
        source="/host/awf/auth/ws_launcher/aws",
        target="/home/agent/.aws",
        mode="rw",
    )
    environment = (
        ("CLAUDE_CODE_USE_BEDROCK", "1"),
        ("AWS_PROFILE", "awf-bedrock"),
        (environment_name, f"/home/agent/.aws/../../run/awf/secrets/{profile_file_name}"),
    )

    clarification_mounts = stack_launcher_mod._clarification_auth_mounts(  # noqa: SLF001
        (aws_profile_directory,),
        agent_environment=environment,
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.claude_code,
    )

    assert clarification_mounts == ()


@pytest.mark.unit
def test_clarification_inputs_prefer_explicit_vertex_credentials_to_adc_fallback() -> None:
    """Vertex clarification does not stage inactive ADC beside explicit credentials."""
    gcloud_auth = AuthMount(
        source="/host/awf/auth/ws_launcher/gcloud",
        target="/home/agent/.config/gcloud",
        mode="ro",
    )
    google_credentials = AuthMount(
        source="/host/awf/secret-leases/ws_launcher/google-credentials.json",
        target="/run/awf/secrets/gcp/credentials.json",
        mode="ro",
    )
    environment = (
        ("CLAUDE_CODE_USE_VERTEX", "1"),
        ("ANTHROPIC_VERTEX_PROJECT_ID", "awf-vertex-project"),
        ("GOOGLE_APPLICATION_CREDENTIALS", google_credentials.target),
    )

    clarification_environment = stack_launcher_mod._clarification_agent_environment(  # noqa: SLF001
        environment,
        auth_mounts=(gcloud_auth, google_credentials),
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.claude_code,
    )
    clarification_mounts = stack_launcher_mod._clarification_auth_mounts(  # noqa: SLF001
        (gcloud_auth, google_credentials),
        agent_environment=environment,
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.claude_code,
    )

    assert clarification_environment == (
        ("CLAUDE_CODE_USE_VERTEX", "1"),
        ("ANTHROPIC_VERTEX_PROJECT_ID", "awf-vertex-project"),
        ("GOOGLE_APPLICATION_CREDENTIALS", "/home/agent/.awf/clarification-auth/0"),
    )
    assert clarification_mounts == (
        AuthMount(
            source=google_credentials.source,
            target="/home/agent/.awf/clarification-auth/0",
            mode="ro",
        ),
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("agent_runtime", "environment"),
    (
        (
            AgentRuntime.claude_code,
            (
                ("CLAUDE_CODE_USE_VERTEX", "1"),
                ("ANTHROPIC_VERTEX_PROJECT_ID", "awf-vertex-project"),
            ),
        ),
        (
            AgentRuntime.gemini,
            (("GOOGLE_GENAI_USE_VERTEXAI", "1"),),
        ),
    ),
)
def test_clarification_stages_external_account_adc_subject_token_mount(
    tmp_path: Path,
    agent_runtime: AgentRuntime,
    environment: tuple[tuple[str, str], ...],
) -> None:
    """External-account ADC stages its declared subject-token path with the ADC."""
    subject_token = tmp_path / "subject-token"
    subject_token.write_text("subject-token", encoding="utf-8")
    adc_config = tmp_path / "external-account-adc.json"
    subject_token_target = "/run/awf/secrets/google/subject-token"
    adc_target = "/run/awf/secrets/google/external-account-adc.json"
    adc_config.write_text(
        json.dumps(
            {
                "type": "external_account",
                "credential_source": {"file": subject_token_target},
            }
        ),
        encoding="utf-8",
    )
    adc_mount = AuthMount(source=str(adc_config), target=adc_target, mode="ro")
    subject_token_mount = AuthMount(
        source=str(subject_token), target=subject_token_target, mode="ro"
    )
    agent_environment = (*environment, ("GOOGLE_APPLICATION_CREDENTIALS", adc_target))

    clarification_environment = stack_launcher_mod._clarification_agent_environment(  # noqa: SLF001
        agent_environment,
        auth_mounts=(adc_mount, subject_token_mount),
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=agent_runtime,
    )
    clarification_mounts = stack_launcher_mod._clarification_auth_mounts(  # noqa: SLF001
        (adc_mount, subject_token_mount),
        agent_environment=agent_environment,
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=agent_runtime,
    )

    assert dict(clarification_environment)["GOOGLE_APPLICATION_CREDENTIALS"] == (
        "/home/agent/.awf/clarification-auth/0"
    )
    assert clarification_mounts == (
        AuthMount(
            source=str(adc_config),
            target="/home/agent/.awf/clarification-auth/0",
            mode="ro",
        ),
        AuthMount(
            source=str(subject_token),
            target="/home/agent/.awf/clarification-auth/1",
            mode="ro",
        ),
    )
    assert external_account_subject_token_file_rewrites(
        (adc_mount, subject_token_mount),
        agent_environment=agent_environment,
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=agent_runtime,
    ) == ((subject_token_target, "/home/agent/.awf/clarification-auth/1"),)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("environment_id", "expected_aws_environment"),
    (
        (
            "aws1",
            (
                ("AWS_ACCESS_KEY_ID", "AKIA_PROFILE_IDENTIFIER"),
                ("AWS_SECRET_ACCESS_KEY", "static-secret"),
                ("AWS_SESSION_TOKEN", "static-session-token"),
                ("AWS_REGION", "us-east-1"),
                ("AWS_DEFAULT_REGION", "us-west-2"),
            ),
        ),
        ("azure1", ()),
    ),
)
def test_clarification_retains_aws_inputs_only_for_gemini_aws_external_account_adc(
    tmp_path: Path,
    environment_id: str,
    expected_aws_environment: tuple[tuple[str, str], ...],
) -> None:
    """Only Gemini AWS external-account ADC retains injected AWS credentials."""
    adc_config = tmp_path / "external-account-adc.json"
    adc_target = "/run/awf/secrets/google/external-account-adc.json"
    adc_config.write_text(
        json.dumps(
            {
                "type": "external_account",
                "credential_source": {"environment_id": environment_id},
            }
        ),
        encoding="utf-8",
    )
    adc_mount = AuthMount(source=str(adc_config), target=adc_target, mode="ro")
    environment = (
        ("GOOGLE_GENAI_USE_VERTEXAI", "1"),
        ("GOOGLE_APPLICATION_CREDENTIALS", adc_target),
        ("AWS_ACCESS_KEY_ID", "AKIA_PROFILE_IDENTIFIER"),
        ("AWS_SECRET_ACCESS_KEY", "static-secret"),
        ("AWS_SESSION_TOKEN", "static-session-token"),
        ("AWS_REGION", "us-east-1"),
        ("AWS_DEFAULT_REGION", "us-west-2"),
        ("AWS_PROFILE", "must-not-leak"),
    )

    clarification_environment = stack_launcher_mod._clarification_agent_environment(  # noqa: SLF001
        environment,
        auth_mounts=(adc_mount,),
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.gemini,
    )

    assert (
        clarification_environment
        == (
            ("GOOGLE_GENAI_USE_VERTEXAI", "1"),
            ("GOOGLE_APPLICATION_CREDENTIALS", "/home/agent/.awf/clarification-auth/0"),
        )
        + expected_aws_environment
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("agent_runtime", "environment"),
    (
        (
            AgentRuntime.claude_code,
            (
                ("CLAUDE_CODE_USE_VERTEX", "1"),
                ("ANTHROPIC_VERTEX_PROJECT_ID", "awf-vertex-project"),
            ),
        ),
        (
            AgentRuntime.gemini,
            (("GOOGLE_GENAI_USE_VERTEXAI", "1"),),
        ),
    ),
)
def test_clarification_stages_external_account_executable_source_mounts(
    tmp_path: Path,
    agent_runtime: AgentRuntime,
    environment: tuple[tuple[str, str], ...],
) -> None:
    """Executable external-account ADC sources retain their helper and output."""
    helper = tmp_path / "external-account-helper"
    helper.write_text("#!/bin/sh\n", encoding="utf-8")
    output = tmp_path / "external-account-output.json"
    output.write_text("{}", encoding="utf-8")
    adc_config = tmp_path / "external-account-adc.json"
    helper_target = "/run/awf/secrets/google/external-account-helper"
    non_normalized_helper_target = "/run/awf/secrets/google/./external-account-helper"
    output_target = "/run/awf/secrets/google/external-account-output.json"
    adc_target = "/run/awf/secrets/google/external-account-adc.json"
    adc_config.write_text(
        json.dumps(
            {
                "type": "external_account",
                "credential_source": {
                    "executable": {
                        "command": f"{non_normalized_helper_target} --output {output_target}",
                        "output_file": output_target,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    adc_mount = AuthMount(source=str(adc_config), target=adc_target, mode="ro")
    helper_mount = AuthMount(source=str(helper), target=helper_target, mode="ro")
    output_mount = AuthMount(source=str(output), target=output_target, mode="ro")
    agent_environment = (
        *environment,
        ("GOOGLE_APPLICATION_CREDENTIALS", adc_target),
        ("GOOGLE_EXTERNAL_ACCOUNT_ALLOW_EXECUTABLES", "1"),
    )

    clarification_environment = stack_launcher_mod._clarification_agent_environment(  # noqa: SLF001
        agent_environment,
        auth_mounts=(adc_mount, helper_mount, output_mount),
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=agent_runtime,
    )
    clarification_mounts = stack_launcher_mod._clarification_auth_mounts(  # noqa: SLF001
        (adc_mount, helper_mount, output_mount),
        agent_environment=agent_environment,
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=agent_runtime,
    )

    assert dict(clarification_environment)["GOOGLE_APPLICATION_CREDENTIALS"] == (
        "/home/agent/.awf/clarification-auth/0"
    )
    assert dict(clarification_environment)["GOOGLE_EXTERNAL_ACCOUNT_ALLOW_EXECUTABLES"] == "1"
    assert clarification_mounts == (
        AuthMount(
            source=str(adc_config),
            target="/home/agent/.awf/clarification-auth/0",
            mode="ro",
        ),
        AuthMount(
            source=str(helper),
            target="/home/agent/.awf/clarification-auth/1",
            mode="ro",
        ),
        AuthMount(
            source=str(output),
            target="/home/agent/.awf/clarification-auth/2",
            mode="ro",
        ),
    )
    assert external_account_subject_token_file_rewrites(
        (adc_mount, helper_mount, output_mount),
        agent_environment=agent_environment,
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=agent_runtime,
    ) == (
        (helper_target, "/home/agent/.awf/clarification-auth/1"),
        (output_target, "/home/agent/.awf/clarification-auth/2"),
    )


@pytest.mark.unit
def test_clarification_stages_transitive_bedrock_profile_auth_mounts(tmp_path: Path) -> None:
    """Bedrock profile helpers and web-identity tokens follow the staged profile."""
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
