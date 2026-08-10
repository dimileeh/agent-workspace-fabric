"""Stack launcher hosted render-only edge tests."""

from __future__ import annotations

import pytest

from awf.db.enums import AgentRuntime
from awf.node import stack_launcher as stack_launcher_mod
from awf.node.compose_manager import AuthMount


@pytest.mark.unit
def test_clarification_inputs_retain_only_selected_adapter_credentials() -> None:
    """Codex clarification cannot read credentials for other coding adapters."""
    mirror = "/host/awf/git/mirrors/repo.git"
    codex_auth = AuthMount(
        source="/host/awf/auth/ws_launcher/codex",
        target="/home/agent/.codex",
        mode="rw",
    )
    provider_credentials = AuthMount(
        source="/host/awf/secret-leases/ws_launcher/google-credentials.json",
        target="/run/awf/secrets/gcp/credentials.json",
        mode="ro",
    )
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
    database_credentials = AuthMount(
        source="/host/awf/secret-leases/ws_launcher/database-password",
        target="/run/awf/secrets/database-password",
        mode="ro",
    )
    bitbucket_askpass = AuthMount(
        source="/host/awf/secret-leases/ws_launcher/bb-askpass.sh",
        target="/run/awf/secrets/bb-askpass.sh",
        mode="ro",
    )

    auth_mounts = (
        AuthMount(source=mirror, target=mirror, mode="rw"),
        codex_auth,
        provider_credentials,
        aws_shared_credentials,
        aws_config,
        aws_web_identity_token,
        database_credentials,
        bitbucket_askpass,
        AuthMount(
            source="/home/agent/.config/gh",
            target="/home/agent/.config/gh",
            mode="ro",
        ),
        AuthMount(
            source="/home/agent/.gitconfig",
            target="/home/agent/.gitconfig",
            mode="ro",
        ),
        AuthMount(source="/home/agent/.ssh", target="/home/agent/.ssh", mode="ro"),
    )
    agent_environment = (
        ("OPENAI_API_KEY", "${OPENAI_API_KEY}"),
        ("CLAUDE_CODE_USE_BEDROCK", "1"),
        ("AWS_ACCESS_KEY_ID", "AKIA_PROFILE_IDENTIFIER"),
        ("AWS_SECRET_ACCESS_KEY", "profile-secret"),
        ("AWS_SESSION_TOKEN", "profile-session-token"),
        ("AWS_REGION", "us-west-2"),
        ("AWS_DEFAULT_REGION", "us-west-2"),
        ("AWS_PROFILE", "awf-bedrock"),
        ("AWS_SHARED_CREDENTIALS_FILE", aws_shared_credentials.target),
        ("AWS_CONFIG_FILE", aws_config.target),
        ("AWS_ROLE_ARN", "arn:aws:iam::123456789012:role/awf-bedrock"),
        ("AWS_WEB_IDENTITY_TOKEN_FILE", aws_web_identity_token.target),
        ("AWS_BEARER_TOKEN_BEDROCK", "profile-bedrock-token"),
        ("CLAUDE_CODE_USE_VERTEX", "1"),
        ("ANTHROPIC_VERTEX_PROJECT_ID", "awf-vertex-project"),
        ("CLOUD_ML_REGION", "us-central1"),
        ("GOOGLE_APPLICATION_CREDENTIALS", "/run/awf/secrets/gcp/credentials.json"),
        ("AWF_DATABASE_URL", "postgresql+asyncpg://awf@postgres:5432/awf"),
        ("AWF_TEST_DATABASE_URL", "postgresql+asyncpg://awf@postgres:5432/awf"),
        ("DOCKER_HOST", "tcp://docker:2375"),
        ("SERVICE_TOKEN", "workspace-service-token"),
        ("GIT_ASKPASS", "/run/awf/secrets/bb-askpass.sh"),
        ("GH_TOKEN", "${AWF_GITHUB_TOKEN}"),
        ("GITHUB_TOKEN", "${AWF_GITHUB_TOKEN}"),
        ("BITBUCKET_API_TOKEN", "${BITBUCKET_API_TOKEN}"),
        ("SSH_AUTH_SOCK", "/run/ssh-agent.sock"),
    )
    mounts = stack_launcher_mod._clarification_auth_mounts(  # noqa: SLF001
        auth_mounts,
        agent_environment=agent_environment,
        mirror_target=mirror,
        agent_runtime=AgentRuntime.codex,
    )
    environment = stack_launcher_mod._clarification_agent_environment(  # noqa: SLF001
        agent_environment,
        auth_mounts=auth_mounts,
        mirror_target=mirror,
        agent_runtime=AgentRuntime.codex,
    )

    assert environment == ()
    assert mounts == (AuthMount(source=codex_auth.source, target=codex_auth.target, mode="ro"),)


@pytest.mark.unit
def test_codex_clarification_prefers_file_auth_to_environment_credentials() -> None:
    """Codex file auth wins over environment credentials for clarification."""
    codex_auth = AuthMount(
        source="/host/awf/auth/ws_launcher/codex",
        target="/home/agent/.codex",
        mode="rw",
    )
    environment = (
        ("OPENAI_API_KEY", "api-key"),
        ("CODEX_AUTH_TOKEN", "auth-token"),
        ("OPENAI_BASE_URL", "https://openai.example.test/v1"),
    )

    clarification_environment = stack_launcher_mod._clarification_agent_environment(  # noqa: SLF001
        environment,
        auth_mounts=(codex_auth,),
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.codex,
    )
    clarification_mounts = stack_launcher_mod._clarification_auth_mounts(  # noqa: SLF001
        (codex_auth,),
        agent_environment=environment,
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.codex,
    )

    assert clarification_environment == (("OPENAI_BASE_URL", "https://openai.example.test/v1"),)
    assert clarification_mounts == (
        AuthMount(source=codex_auth.source, target=codex_auth.target, mode="ro"),
    )


@pytest.mark.unit
def test_grok_clarification_prefers_api_key_to_cached_token_auth() -> None:
    """Grok clarification excludes inactive cached-token auth when an API key is set."""
    grok_auth = AuthMount(
        source="/host/awf/auth/ws_launcher/grok",
        target="/home/agent/.grok",
        mode="rw",
    )
    xai_credentials = AuthMount(
        source="/host/awf/secret-leases/ws_launcher/xai-key",
        target="/run/awf/secrets/xai-key",
        mode="ro",
    )
    environment = (("XAI_API_KEY", xai_credentials.target),)

    clarification_environment = stack_launcher_mod._clarification_agent_environment(  # noqa: SLF001
        environment,
        auth_mounts=(grok_auth, xai_credentials),
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.grok,
    )
    clarification_mounts = stack_launcher_mod._clarification_auth_mounts(  # noqa: SLF001
        (grok_auth, xai_credentials),
        agent_environment=environment,
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.grok,
    )

    assert clarification_environment == (("XAI_API_KEY", "/home/agent/.awf/clarification-auth/0"),)
    assert clarification_mounts == (
        AuthMount(
            source=xai_credentials.source,
            target="/home/agent/.awf/clarification-auth/0",
            mode="ro",
        ),
    )


@pytest.mark.unit
def test_grok_clarification_uses_cached_token_auth_without_an_api_key() -> None:
    """Grok clarification retains cached-token auth when no API key is available."""
    grok_auth = AuthMount(
        source="/host/awf/auth/ws_launcher/grok",
        target="/home/agent/.grok",
        mode="rw",
    )

    clarification_mounts = stack_launcher_mod._clarification_auth_mounts(  # noqa: SLF001
        (grok_auth,),
        agent_environment=(),
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.grok,
    )

    assert clarification_mounts == (
        AuthMount(source=grok_auth.source, target=grok_auth.target, mode="ro"),
    )


@pytest.mark.unit
def test_grok_clarification_uses_cached_token_auth_when_optional_api_key_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty Compose API-key placeholder does not suppress Grok file auth."""
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    grok_auth = AuthMount(
        source="/host/awf/auth/ws_launcher/grok",
        target="/home/agent/.grok",
        mode="rw",
    )

    clarification_mounts = stack_launcher_mod._clarification_auth_mounts(  # noqa: SLF001
        (grok_auth,),
        agent_environment=(("XAI_API_KEY", "${XAI_API_KEY:-}"),),
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.grok,
    )

    assert clarification_mounts == (
        AuthMount(source=grok_auth.source, target=grok_auth.target, mode="ro"),
    )


@pytest.mark.unit
def test_clarification_inputs_retain_selected_claude_backend_credentials() -> None:
    """Bedrock and Vertex clarification excludes inactive direct Claude auth."""
    claude_auth = AuthMount(
        source="/host/awf/auth/ws_launcher/claude",
        target="/home/agent/.claude",
        mode="rw",
    )
    aws_credentials = AuthMount(
        source="/host/awf/secret-leases/ws_launcher/aws-credentials",
        target="/run/awf/secrets/aws-credentials",
        mode="ro",
    )
    google_credentials = AuthMount(
        source="/host/awf/secret-leases/ws_launcher/google-credentials.json",
        target="/run/awf/secrets/gcp/credentials.json",
        mode="ro",
    )
    environment = (
        ("OPENAI_API_KEY", "unrelated-openai-token"),
        ("ANTHROPIC_API_KEY", "anthropic-token"),
        ("ANTHROPIC_AUTH_TOKEN", "anthropic-auth-token"),
        ("ANTHROPIC_BASE_URL", "https://anthropic.example.test"),
        ("ANTHROPIC_SMALL_FAST_MODEL", "claude-fast"),
        ("CLAUDE_CODE_OAUTH_TOKEN", "claude-oauth-token"),
        ("CLAUDE_CODE_USE_BEDROCK", "1"),
        ("AWS_SECRET_ACCESS_KEY", "bedrock-secret"),
        ("AWS_SHARED_CREDENTIALS_FILE", aws_credentials.target),
        ("CLAUDE_CODE_USE_VERTEX", "1"),
        ("ANTHROPIC_VERTEX_PROJECT_ID", "awf-vertex-project"),
        ("GOOGLE_APPLICATION_CREDENTIALS", google_credentials.target),
    )
    mounts = (claude_auth, aws_credentials, google_credentials)

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
        ("AWS_SHARED_CREDENTIALS_FILE", "/home/agent/.awf/clarification-auth/0"),
        ("CLAUDE_CODE_USE_VERTEX", "1"),
        ("ANTHROPIC_VERTEX_PROJECT_ID", "awf-vertex-project"),
        ("GOOGLE_APPLICATION_CREDENTIALS", "/home/agent/.awf/clarification-auth/1"),
    )
    assert clarification_mounts == (
        AuthMount(
            source=aws_credentials.source,
            target="/home/agent/.awf/clarification-auth/0",
            mode="ro",
        ),
        AuthMount(
            source=google_credentials.source,
            target="/home/agent/.awf/clarification-auth/1",
            mode="ro",
        ),
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
def test_clarification_bedrock_profile_directory_excludes_external_shared_credentials_file() -> (
    None
):
    """An external Bedrock credentials file does not select the standard directory."""
    aws_profile_directory = AuthMount(
        source="/host/awf/auth/ws_launcher/aws",
        target="/home/agent/.aws",
        mode="rw",
    )
    environment = (
        ("CLAUDE_CODE_USE_BEDROCK", "1"),
        ("AWS_PROFILE", "awf-bedrock"),
        ("AWS_SHARED_CREDENTIALS_FILE", "/run/awf/secrets/aws-credentials"),
    )

    clarification_mounts = stack_launcher_mod._clarification_auth_mounts(  # noqa: SLF001
        (aws_profile_directory,),
        agent_environment=environment,
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.claude_code,
    )

    assert clarification_mounts == ()


@pytest.mark.unit
def test_clarification_bedrock_profile_directory_excludes_external_config_file() -> None:
    """An external Bedrock config file does not select the standard directory."""
    aws_profile_directory = AuthMount(
        source="/host/awf/auth/ws_launcher/aws",
        target="/home/agent/.aws",
        mode="rw",
    )
    environment = (
        ("CLAUDE_CODE_USE_BEDROCK", "1"),
        ("AWS_PROFILE", "awf-bedrock"),
        ("AWS_CONFIG_FILE", "/run/awf/secrets/aws-config"),
    )

    clarification_mounts = stack_launcher_mod._clarification_auth_mounts(  # noqa: SLF001
        (aws_profile_directory,),
        agent_environment=environment,
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.claude_code,
    )

    assert clarification_mounts == ()


@pytest.mark.unit
def test_clarification_bedrock_profile_directory_excludes_traversal_config_file() -> None:
    """A traversal config path does not select the standard AWS directory."""
    aws_profile_directory = AuthMount(
        source="/host/awf/auth/ws_launcher/aws",
        target="/home/agent/.aws",
        mode="rw",
    )
    environment = (
        ("CLAUDE_CODE_USE_BEDROCK", "1"),
        ("AWS_PROFILE", "awf-bedrock"),
        ("AWS_CONFIG_FILE", "/home/agent/.aws/../../run/awf/secrets/aws-config"),
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


@pytest.mark.unit
@pytest.mark.parametrize(
    ("agent_runtime", "environment"),
    (
        (
            AgentRuntime.claude_code,
            (
                ("CLAUDE_CODE_USE_VERTEX", "1"),
                ("ANTHROPIC_VERTEX_PROJECT_ID", "awf-vertex-project"),
                (
                    "GOOGLE_APPLICATION_CREDENTIALS",
                    "/home/agent/.config/gcloud/awf-service-account.json",
                ),
            ),
        ),
        (
            AgentRuntime.gemini,
            (
                ("GOOGLE_GENAI_USE_VERTEXAI", "true"),
                ("GOOGLE_CLOUD_PROJECT", "awf-project"),
                (
                    "GOOGLE_APPLICATION_CREDENTIALS",
                    "/home/agent/.config/gcloud/awf-service-account.json",
                ),
            ),
        ),
    ),
)
def test_clarification_inputs_retain_gcloud_mount_for_contained_credentials(
    agent_runtime: AgentRuntime,
    environment: tuple[tuple[str, str], ...],
) -> None:
    """A credential below the gcloud mount retains its parent during clarification."""
    gcloud_auth = AuthMount(
        source="/host/awf/auth/ws_launcher/gcloud",
        target="/home/agent/.config/gcloud",
        mode="ro",
    )
    mirror_target = "/host/awf/git/mirrors/repo.git"

    assert (
        stack_launcher_mod._clarification_agent_environment(  # noqa: SLF001
            environment,
            auth_mounts=(gcloud_auth,),
            mirror_target=mirror_target,
            agent_runtime=agent_runtime,
        )
        == environment
    )
    assert stack_launcher_mod._clarification_auth_mounts(  # noqa: SLF001
        (gcloud_auth,),
        agent_environment=environment,
        mirror_target=mirror_target,
        agent_runtime=agent_runtime,
    ) == (gcloud_auth,)


@pytest.mark.unit
def test_clarification_inputs_prefer_claude_file_auth_to_direct_credentials() -> None:
    """Claude file auth wins over direct credentials for clarification."""
    claude_auth = AuthMount(
        source="/host/awf/auth/ws_launcher/claude",
        target="/home/agent/.claude",
        mode="rw",
    )
    aws_credentials = AuthMount(
        source="/host/awf/secret-leases/ws_launcher/aws-credentials",
        target="/run/awf/secrets/aws-credentials",
        mode="ro",
    )
    environment = (
        ("ANTHROPIC_API_KEY", "anthropic-token"),
        ("ANTHROPIC_AUTH_TOKEN", "anthropic-auth-token"),
        ("ANTHROPIC_BASE_URL", "https://anthropic.example.test"),
        ("ANTHROPIC_SMALL_FAST_MODEL", "claude-fast"),
        ("CLAUDE_CODE_OAUTH_TOKEN", "claude-oauth-token"),
        ("CLAUDE_CODE_USE_BEDROCK", "0"),
        ("CLAUDE_CODE_USE_VERTEX", "0"),
        ("AWS_SHARED_CREDENTIALS_FILE", aws_credentials.target),
    )

    clarification_environment = stack_launcher_mod._clarification_agent_environment(  # noqa: SLF001
        environment,
        auth_mounts=(claude_auth, aws_credentials),
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.claude_code,
    )
    clarification_mounts = stack_launcher_mod._clarification_auth_mounts(  # noqa: SLF001
        (claude_auth, aws_credentials),
        agent_environment=environment,
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.claude_code,
    )

    assert clarification_environment == (
        ("ANTHROPIC_BASE_URL", "https://anthropic.example.test"),
        ("ANTHROPIC_SMALL_FAST_MODEL", "claude-fast"),
        ("CLAUDE_CODE_USE_BEDROCK", "0"),
        ("CLAUDE_CODE_USE_VERTEX", "0"),
    )
    assert clarification_mounts == (
        AuthMount(source=claude_auth.source, target=claude_auth.target, mode="ro"),
    )


@pytest.mark.unit
def test_gemini_clarification_selects_only_its_active_credential_source() -> None:
    """Gemini re-asks do not stage inactive Google or CLI-file credentials."""
    api_key = AuthMount(
        source="/host/awf/secret-leases/ws_launcher/gemini-api-key",
        target="/run/awf/secrets/gemini-api-key",
        mode="ro",
    )
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
    application_credentials = AuthMount(
        source="/host/awf/secret-leases/ws_launcher/google-credentials.json",
        target="/run/awf/secrets/gcp/credentials.json",
        mode="ro",
    )
    mounts = (api_key, gcloud_auth, gemini_auth, application_credentials)
    mirror_target = "/host/awf/git/mirrors/repo.git"

    api_key_environment = (
        ("GEMINI_API_KEY_AUTH_MECHANISM", "api-key"),
        ("GEMINI_API_KEY", api_key.target),
        ("GOOGLE_GENAI_USE_VERTEXAI", "true"),
        ("GOOGLE_APPLICATION_CREDENTIALS", application_credentials.target),
    )
    assert stack_launcher_mod._clarification_agent_environment(  # noqa: SLF001
        api_key_environment,
        auth_mounts=mounts,
        mirror_target=mirror_target,
        agent_runtime=AgentRuntime.gemini,
    ) == (
        ("GEMINI_API_KEY_AUTH_MECHANISM", "api-key"),
        ("GEMINI_API_KEY", "/home/agent/.awf/clarification-auth/0"),
    )
    assert stack_launcher_mod._clarification_auth_mounts(  # noqa: SLF001
        mounts,
        agent_environment=api_key_environment,
        mirror_target=mirror_target,
        agent_runtime=AgentRuntime.gemini,
    ) == (
        AuthMount(
            source=api_key.source,
            target="/home/agent/.awf/clarification-auth/0",
            mode="ro",
        ),
    )

    vertex_environment = (
        ("GOOGLE_GENAI_USE_VERTEXAI", "true"),
        ("GOOGLE_CLOUD_PROJECT", "awf-project"),
        ("GOOGLE_CLOUD_LOCATION", "us-central1"),
        ("GOOGLE_APPLICATION_CREDENTIALS", application_credentials.target),
    )
    assert stack_launcher_mod._clarification_agent_environment(  # noqa: SLF001
        vertex_environment,
        auth_mounts=mounts,
        mirror_target=mirror_target,
        agent_runtime=AgentRuntime.gemini,
    ) == (
        ("GOOGLE_GENAI_USE_VERTEXAI", "true"),
        ("GOOGLE_CLOUD_PROJECT", "awf-project"),
        ("GOOGLE_CLOUD_LOCATION", "us-central1"),
        ("GOOGLE_APPLICATION_CREDENTIALS", "/home/agent/.awf/clarification-auth/0"),
    )
    assert stack_launcher_mod._clarification_auth_mounts(  # noqa: SLF001
        mounts,
        agent_environment=vertex_environment,
        mirror_target=mirror_target,
        agent_runtime=AgentRuntime.gemini,
    ) == (
        AuthMount(
            source=application_credentials.source,
            target="/home/agent/.awf/clarification-auth/0",
            mode="ro",
        ),
    )

    vertex_adc_environment = (
        ("GOOGLE_GENAI_USE_VERTEXAI", "true"),
        ("GOOGLE_CLOUD_PROJECT", "awf-project"),
        ("GOOGLE_CLOUD_LOCATION", "us-central1"),
    )
    assert stack_launcher_mod._clarification_auth_mounts(  # noqa: SLF001
        mounts,
        agent_environment=vertex_adc_environment,
        mirror_target=mirror_target,
        agent_runtime=AgentRuntime.gemini,
    ) == (AuthMount(source=gcloud_auth.source, target=gcloud_auth.target, mode="ro"),)

    vertex_explicit_adc_environment = vertex_adc_environment + (
        (
            "GOOGLE_APPLICATION_CREDENTIALS",
            "/home/agent/.config/gcloud/application_default_credentials.json",
        ),
    )
    assert (
        stack_launcher_mod._clarification_agent_environment(  # noqa: SLF001
            vertex_explicit_adc_environment,
            auth_mounts=mounts,
            mirror_target=mirror_target,
            agent_runtime=AgentRuntime.gemini,
        )
        == vertex_explicit_adc_environment
    )
    assert stack_launcher_mod._clarification_auth_mounts(  # noqa: SLF001
        mounts,
        agent_environment=vertex_explicit_adc_environment,
        mirror_target=mirror_target,
        agent_runtime=AgentRuntime.gemini,
    ) == (AuthMount(source=gcloud_auth.source, target=gcloud_auth.target, mode="ro"),)

    assert stack_launcher_mod._clarification_auth_mounts(  # noqa: SLF001
        mounts,
        agent_environment=(),
        mirror_target=mirror_target,
        agent_runtime=AgentRuntime.gemini,
    ) == (AuthMount(source=gemini_auth.source, target=gemini_auth.target, mode="ro"),)
    assert (
        stack_launcher_mod._clarification_gemini_auth_source(  # noqa: SLF001
            {"GEMINI_API_KEY": "api-key-without-selector"}
        )
        == "api_key"
    )


@pytest.mark.unit
def test_gemini_clarification_expands_optional_api_keys_before_auth_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unset optional Compose key falls back to Gemini CLI-file auth."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    assert (
        stack_launcher_mod._clarification_gemini_auth_source(  # noqa: SLF001
            {"GEMINI_API_KEY": "${GEMINI_API_KEY:-}"}
        )
        == "file"
    )


@pytest.mark.unit
def test_gemini_clarification_expands_auth_mechanism_before_vertex_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy host-auth API-key selection takes precedence over Vertex."""
    monkeypatch.setenv("GEMINI_API_KEY_AUTH_MECHANISM", "api-key")
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")

    assert (
        stack_launcher_mod._clarification_gemini_auth_source(  # noqa: SLF001
            {
                "GEMINI_API_KEY_AUTH_MECHANISM": "${GEMINI_API_KEY_AUTH_MECHANISM}",
                "GOOGLE_GENAI_USE_VERTEXAI": "${GOOGLE_GENAI_USE_VERTEXAI}",
            }
        )
        == "api_key"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("toggle", "enabled_value"),
    (("GOOGLE_GENAI_USE_VERTEXAI", "true"), ("GOOGLE_GENAI_USE_GCA", "yes")),
)
def test_gemini_clarification_expands_optional_access_token_before_auth_selection(
    monkeypatch: pytest.MonkeyPatch,
    toggle: str,
    enabled_value: str,
) -> None:
    """An unset optional token does not override enabled Google Cloud auth."""
    monkeypatch.delenv("GOOGLE_CLOUD_ACCESS_TOKEN", raising=False)

    assert (
        stack_launcher_mod._clarification_gemini_auth_source(  # noqa: SLF001
            {
                "GOOGLE_CLOUD_ACCESS_TOKEN": "${GOOGLE_CLOUD_ACCESS_TOKEN:-}",
                toggle: enabled_value,
            }
        )
        == "google_cloud"
    )


@pytest.mark.unit
def test_gemini_clarification_resolves_google_credentials_compose_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Vertex clarification stages the dynamic ADC mount behind a Compose token."""
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    gcloud_auth = AuthMount(
        source="/host/awf/auth/ws_launcher/gcloud",
        target="/home/agent/.config/gcloud",
        mode="ro",
    )
    google_credentials = AuthMount(
        source="/host/awf/auth/ws_launcher/google-credentials.json",
        target="/host/awf/auth/ws_launcher/google-credentials.json",
        mode="ro",
    )
    environment = (
        ("GOOGLE_GENAI_USE_VERTEXAI", "true"),
        ("GOOGLE_CLOUD_PROJECT", "awf-project"),
        ("GOOGLE_APPLICATION_CREDENTIALS", "${GOOGLE_APPLICATION_CREDENTIALS}"),
    )

    assert stack_launcher_mod._clarification_agent_environment(  # noqa: SLF001
        environment,
        auth_mounts=(gcloud_auth, google_credentials),
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.gemini,
    ) == (
        ("GOOGLE_GENAI_USE_VERTEXAI", "true"),
        ("GOOGLE_CLOUD_PROJECT", "awf-project"),
        ("GOOGLE_APPLICATION_CREDENTIALS", "/home/agent/.awf/clarification-auth/0"),
    )
    assert stack_launcher_mod._clarification_auth_mounts(  # noqa: SLF001
        (gcloud_auth, google_credentials),
        agent_environment=environment,
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.gemini,
    ) == (
        AuthMount(
            source=google_credentials.source,
            target="/home/agent/.awf/clarification-auth/0",
            mode="ro",
        ),
    )


@pytest.mark.unit
def test_claude_vertex_clarification_resolves_google_credentials_compose_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Vertex clarification stages the dynamic ADC mount behind a Compose token."""
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    gcloud_auth = AuthMount(
        source="/host/awf/auth/ws_launcher/gcloud",
        target="/home/agent/.config/gcloud",
        mode="ro",
    )
    google_credentials = AuthMount(
        source="/host/awf/auth/ws_launcher/google-credentials.json",
        target="/host/awf/auth/ws_launcher/google-credentials.json",
        mode="ro",
    )
    environment = (
        ("CLAUDE_CODE_USE_VERTEX", "1"),
        ("ANTHROPIC_VERTEX_PROJECT_ID", "awf-project"),
        ("GOOGLE_APPLICATION_CREDENTIALS", "${GOOGLE_APPLICATION_CREDENTIALS}"),
    )

    assert stack_launcher_mod._clarification_agent_environment(  # noqa: SLF001
        environment,
        auth_mounts=(gcloud_auth, google_credentials),
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.claude_code,
    ) == (
        ("CLAUDE_CODE_USE_VERTEX", "1"),
        ("ANTHROPIC_VERTEX_PROJECT_ID", "awf-project"),
        ("GOOGLE_APPLICATION_CREDENTIALS", "/home/agent/.awf/clarification-auth/0"),
    )
    assert stack_launcher_mod._clarification_auth_mounts(  # noqa: SLF001
        (gcloud_auth, google_credentials),
        agent_environment=environment,
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.claude_code,
    ) == (
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
            AgentRuntime.gemini,
            (
                ("GOOGLE_GENAI_USE_VERTEXAI", "true"),
                ("GOOGLE_CLOUD_PROJECT", "awf-project"),
            ),
        ),
        (
            AgentRuntime.claude_code,
            (
                ("CLAUDE_CODE_USE_VERTEX", "1"),
                ("ANTHROPIC_VERTEX_PROJECT_ID", "awf-project"),
            ),
        ),
    ),
)
def test_google_vertex_clarification_resolves_bare_google_credentials_placeholder_with_other_pass_through_mount(
    monkeypatch: pytest.MonkeyPatch,
    agent_runtime: AgentRuntime,
    environment: tuple[tuple[str, str], ...],
) -> None:
    """The worker's ADC placeholder selects its mount among pass-through mounts."""
    credentials_target = "/run/awf/secrets/worker-gcp.json"
    google_credentials = AuthMount(
        source=credentials_target,
        target=credentials_target,
        mode="ro",
    )
    unrelated_pass_through = AuthMount(
        source="/run/awf/secrets/unrelated",
        target="/run/awf/secrets/unrelated",
        mode="ro",
    )
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", credentials_target)
    environment += (("GOOGLE_APPLICATION_CREDENTIALS", "${GOOGLE_APPLICATION_CREDENTIALS}"),)

    assert (
        stack_launcher_mod._clarification_agent_environment(  # noqa: SLF001
            environment,
            auth_mounts=(google_credentials, unrelated_pass_through),
            mirror_target="/host/awf/git/mirrors/repo.git",
            agent_runtime=agent_runtime,
        )
        == environment[:-1]
        + (("GOOGLE_APPLICATION_CREDENTIALS", "/home/agent/.awf/clarification-auth/0"),)
    )
    assert stack_launcher_mod._clarification_auth_mounts(  # noqa: SLF001
        (google_credentials, unrelated_pass_through),
        agent_environment=environment,
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=agent_runtime,
    ) == (
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
            AgentRuntime.gemini,
            (
                ("GOOGLE_GENAI_USE_VERTEXAI", "true"),
                ("GOOGLE_CLOUD_PROJECT", "awf-project"),
            ),
        ),
        (
            AgentRuntime.claude_code,
            (
                ("CLAUDE_CODE_USE_VERTEX", "1"),
                ("ANTHROPIC_VERTEX_PROJECT_ID", "awf-project"),
            ),
        ),
    ),
)
def test_google_vertex_clarification_does_not_select_lone_pass_through_mount_for_stale_worker_path(
    monkeypatch: pytest.MonkeyPatch,
    agent_runtime: AgentRuntime,
    environment: tuple[tuple[str, str], ...],
) -> None:
    """An unmatched worker ADC path must not select an unrelated lone mount."""
    unrelated_pass_through = AuthMount(
        source="/run/awf/secrets/unrelated",
        target="/run/awf/secrets/unrelated",
        mode="ro",
    )
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/tmp/stale-worker-gcp.json")
    environment += (("GOOGLE_APPLICATION_CREDENTIALS", "${GOOGLE_APPLICATION_CREDENTIALS}"),)

    assert (
        stack_launcher_mod._clarification_agent_environment(  # noqa: SLF001
            environment,
            auth_mounts=(unrelated_pass_through,),
            mirror_target="/host/awf/git/mirrors/repo.git",
            agent_runtime=agent_runtime,
        )
        == environment
    )
    assert (
        stack_launcher_mod._clarification_auth_mounts(  # noqa: SLF001
            (unrelated_pass_through,),
            agent_environment=environment,
            mirror_target="/host/awf/git/mirrors/repo.git",
            agent_runtime=agent_runtime,
        )
        == ()
    )


@pytest.mark.unit
@pytest.mark.parametrize("operator", (":-", "-"))
@pytest.mark.parametrize(
    "worker_credentials_target",
    (None, "/run/awf/secrets/worker-gcp.json"),
)
@pytest.mark.parametrize(
    ("agent_runtime", "environment"),
    (
        (
            AgentRuntime.gemini,
            (
                ("GOOGLE_GENAI_USE_VERTEXAI", "true"),
                ("GOOGLE_CLOUD_PROJECT", "awf-project"),
            ),
        ),
        (
            AgentRuntime.claude_code,
            (
                ("CLAUDE_CODE_USE_VERTEX", "1"),
                ("ANTHROPIC_VERTEX_PROJECT_ID", "awf-project"),
            ),
        ),
    ),
)
def test_google_vertex_clarification_resolves_defaulted_google_credentials_compose_placeholder(
    agent_runtime: AgentRuntime,
    environment: tuple[tuple[str, str], ...],
    operator: str,
    worker_credentials_target: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Vertex clarification stages the credential selected by a defaulted ADC expression."""
    fallback_target = "/run/awf/secrets/gcp.json"
    google_credentials_target = worker_credentials_target or fallback_target
    if worker_credentials_target is None:
        monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    else:
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", worker_credentials_target)
    gcloud_auth = AuthMount(
        source="/host/awf/auth/ws_launcher/gcloud",
        target="/home/agent/.config/gcloud",
        mode="ro",
    )
    google_credentials = AuthMount(
        source="/host/awf/secret-leases/ws_launcher/google-credentials.json",
        target=google_credentials_target,
        mode="ro",
    )
    environment += (
        (
            "GOOGLE_APPLICATION_CREDENTIALS",
            f"${{GOOGLE_APPLICATION_CREDENTIALS{operator}{fallback_target}}}",
        ),
    )
    auth_mounts = (gcloud_auth, google_credentials)
    mirror_target = "/host/awf/git/mirrors/repo.git"

    assert (
        stack_launcher_mod._clarification_agent_environment(  # noqa: SLF001
            environment,
            auth_mounts=auth_mounts,
            mirror_target=mirror_target,
            agent_runtime=agent_runtime,
        )
        == environment[:-1]
        + (("GOOGLE_APPLICATION_CREDENTIALS", "/home/agent/.awf/clarification-auth/0"),)
    )
    assert stack_launcher_mod._clarification_auth_mounts(  # noqa: SLF001
        auth_mounts,
        agent_environment=environment,
        mirror_target=mirror_target,
        agent_runtime=agent_runtime,
    ) == (
        AuthMount(
            source=google_credentials.source,
            target="/home/agent/.awf/clarification-auth/0",
            mode="ro",
        ),
    )


@pytest.mark.unit
@pytest.mark.parametrize("operator", (":-", "-"))
def test_claude_bedrock_clarification_resolves_defaulted_web_identity_token_placeholder(
    operator: str,
) -> None:
    """Bedrock clarification stages a defaulted web identity token mount."""
    web_identity_token = AuthMount(
        source="/host/awf/secret-leases/ws_launcher/aws-web-identity-token",
        target="/run/awf/secrets/aws-token",
        mode="ro",
    )
    environment = (
        ("CLAUDE_CODE_USE_BEDROCK", "1"),
        ("AWS_ROLE_ARN", "arn:aws:iam::123456789012:role/awf-bedrock"),
        (
            "AWS_WEB_IDENTITY_TOKEN_FILE",
            f"${{AWS_WEB_IDENTITY_TOKEN_FILE{operator}{web_identity_token.target}}}",
        ),
    )
    mirror_target = "/host/awf/git/mirrors/repo.git"

    assert stack_launcher_mod._clarification_agent_environment(  # noqa: SLF001
        environment,
        auth_mounts=(web_identity_token,),
        mirror_target=mirror_target,
        agent_runtime=AgentRuntime.claude_code,
    ) == (
        ("CLAUDE_CODE_USE_BEDROCK", "1"),
        ("AWS_ROLE_ARN", "arn:aws:iam::123456789012:role/awf-bedrock"),
        ("AWS_WEB_IDENTITY_TOKEN_FILE", "/home/agent/.awf/clarification-auth/0"),
    )
    assert stack_launcher_mod._clarification_auth_mounts(  # noqa: SLF001
        (web_identity_token,),
        agent_environment=environment,
        mirror_target=mirror_target,
        agent_runtime=AgentRuntime.claude_code,
    ) == (
        AuthMount(
            source=web_identity_token.source,
            target="/home/agent/.awf/clarification-auth/0",
            mode="ro",
        ),
    )


@pytest.mark.unit
def test_claude_bedrock_clarification_honors_defaulted_web_identity_token_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bedrock clarification stages the web identity token override."""
    fallback_token = AuthMount(
        source="/host/awf/secret-leases/ws_launcher/default-aws-web-identity-token",
        target="/run/awf/secrets/default-token",
        mode="ro",
    )
    override_token = AuthMount(
        source="/host/awf/secret-leases/ws_launcher/override-aws-web-identity-token",
        target="/run/awf/secrets/override-token",
        mode="ro",
    )
    monkeypatch.setenv("AWS_WEB_IDENTITY_TOKEN_FILE", override_token.target)
    environment = (
        ("CLAUDE_CODE_USE_BEDROCK", "1"),
        ("AWS_ROLE_ARN", "arn:aws:iam::123456789012:role/awf-bedrock"),
        (
            "AWS_WEB_IDENTITY_TOKEN_FILE",
            f"${{AWS_WEB_IDENTITY_TOKEN_FILE:-{fallback_token.target}}}",
        ),
    )
    mirror_target = "/host/awf/git/mirrors/repo.git"
    auth_mounts = (fallback_token, override_token)

    assert stack_launcher_mod._clarification_agent_environment(  # noqa: SLF001
        environment,
        auth_mounts=auth_mounts,
        mirror_target=mirror_target,
        agent_runtime=AgentRuntime.claude_code,
    ) == (
        ("CLAUDE_CODE_USE_BEDROCK", "1"),
        ("AWS_ROLE_ARN", "arn:aws:iam::123456789012:role/awf-bedrock"),
        ("AWS_WEB_IDENTITY_TOKEN_FILE", "/home/agent/.awf/clarification-auth/0"),
    )
    assert stack_launcher_mod._clarification_auth_mounts(  # noqa: SLF001
        auth_mounts,
        agent_environment=environment,
        mirror_target=mirror_target,
        agent_runtime=AgentRuntime.claude_code,
    ) == (
        AuthMount(
            source=override_token.source,
            target="/home/agent/.awf/clarification-auth/0",
            mode="ro",
        ),
    )


@pytest.mark.unit
def test_claude_bedrock_clarification_resolves_bare_web_identity_token_placeholder() -> None:
    """Bedrock clarification stages an unambiguous dynamic token mount."""
    web_identity_token = AuthMount(
        source="/run/awf/secrets/aws-token",
        target="/run/awf/secrets/aws-token",
        mode="ro",
    )
    environment = (
        ("CLAUDE_CODE_USE_BEDROCK", "1"),
        ("AWS_ROLE_ARN", "arn:aws:iam::123456789012:role/awf-bedrock"),
        ("AWS_WEB_IDENTITY_TOKEN_FILE", "${AWS_WEB_IDENTITY_TOKEN_FILE}"),
    )
    mirror_target = "/host/awf/git/mirrors/repo.git"

    assert stack_launcher_mod._clarification_agent_environment(  # noqa: SLF001
        environment,
        auth_mounts=(web_identity_token,),
        mirror_target=mirror_target,
        agent_runtime=AgentRuntime.claude_code,
    ) == (
        ("CLAUDE_CODE_USE_BEDROCK", "1"),
        ("AWS_ROLE_ARN", "arn:aws:iam::123456789012:role/awf-bedrock"),
        ("AWS_WEB_IDENTITY_TOKEN_FILE", "/home/agent/.awf/clarification-auth/0"),
    )
    assert stack_launcher_mod._clarification_auth_mounts(  # noqa: SLF001
        (web_identity_token,),
        agent_environment=environment,
        mirror_target=mirror_target,
        agent_runtime=AgentRuntime.claude_code,
    ) == (
        AuthMount(
            source=web_identity_token.source,
            target="/home/agent/.awf/clarification-auth/0",
            mode="ro",
        ),
    )


@pytest.mark.unit
def test_claude_bedrock_clarification_resolves_bare_web_identity_token_from_worker_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bedrock clarification selects the worker's declared token mount."""
    web_identity_token = AuthMount(
        source="/host/awf/secret-leases/ws_launcher/aws-token",
        target="/run/awf/secrets/aws-token",
        mode="ro",
    )
    passthrough_mount = AuthMount(
        source="/home/agent/.gitconfig",
        target="/home/agent/.gitconfig",
        mode="ro",
    )
    monkeypatch.setenv("AWS_WEB_IDENTITY_TOKEN_FILE", web_identity_token.target)
    environment = (
        ("CLAUDE_CODE_USE_BEDROCK", "1"),
        ("AWS_ROLE_ARN", "arn:aws:iam::123456789012:role/awf-bedrock"),
        ("AWS_WEB_IDENTITY_TOKEN_FILE", "${AWS_WEB_IDENTITY_TOKEN_FILE}"),
    )
    mirror_target = "/host/awf/git/mirrors/repo.git"
    auth_mounts = (passthrough_mount, web_identity_token)

    assert stack_launcher_mod._clarification_agent_environment(  # noqa: SLF001
        environment,
        auth_mounts=auth_mounts,
        mirror_target=mirror_target,
        agent_runtime=AgentRuntime.claude_code,
    ) == (
        ("CLAUDE_CODE_USE_BEDROCK", "1"),
        ("AWS_ROLE_ARN", "arn:aws:iam::123456789012:role/awf-bedrock"),
        ("AWS_WEB_IDENTITY_TOKEN_FILE", "/home/agent/.awf/clarification-auth/0"),
    )
    assert stack_launcher_mod._clarification_auth_mounts(  # noqa: SLF001
        auth_mounts,
        agent_environment=environment,
        mirror_target=mirror_target,
        agent_runtime=AgentRuntime.claude_code,
    ) == (
        AuthMount(
            source=web_identity_token.source,
            target="/home/agent/.awf/clarification-auth/0",
            mode="ro",
        ),
    )


@pytest.mark.unit
def test_gemini_clarification_does_not_mount_file_auth_for_access_token() -> None:
    """A direct Google access token is the active Gemini credential source."""
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
    environment = (
        ("GOOGLE_CLOUD_ACCESS_TOKEN", "direct-google-token"),
        ("GOOGLE_CLOUD_PROJECT", "awf-project"),
        ("GOOGLE_CLOUD_LOCATION", "us-central1"),
        ("GOOGLE_GENAI_USE_GCA", "true"),
    )

    assert stack_launcher_mod._clarification_agent_environment(  # noqa: SLF001
        environment,
        auth_mounts=(gcloud_auth, gemini_auth),
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.gemini,
    ) == (
        ("GOOGLE_CLOUD_ACCESS_TOKEN", "direct-google-token"),
        ("GOOGLE_CLOUD_PROJECT", "awf-project"),
        ("GOOGLE_CLOUD_LOCATION", "us-central1"),
    )
    assert (
        stack_launcher_mod._clarification_auth_mounts(  # noqa: SLF001
            (gcloud_auth, gemini_auth),
            agent_environment=environment,
            mirror_target="/host/awf/git/mirrors/repo.git",
            agent_runtime=AgentRuntime.gemini,
        )
        == ()
    )


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
