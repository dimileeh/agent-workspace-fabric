"""No-Docker compose coverage for hosted env safety edge cases (part 9)."""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.profiles import compose as compose_module
from awf.profiles.compose import (
    filter_hosted_env_passthrough_names,
    literal_profile_env_from_compose,
)


@pytest.mark.unit
def test_url_and_secret_like_profile_env_detection_covers_query_credentials() -> None:
    """URL credential detection includes userinfo and query/fragment fields."""

    assert compose_module._is_secret_like_profile_env_name("SERVICE_ACCESS_KEY") is True
    assert compose_module._is_secret_like_profile_env_name("CUSTOM_AUTH_TOKEN") is True
    assert compose_module._is_secret_like_profile_env_name("AUTHORIZATION") is True
    assert compose_module._is_secret_like_profile_env_name("HTTP_AUTHORIZATION") is True
    assert compose_module._is_secret_like_profile_env_name("AUTHORIZATION_URL") is False
    assert compose_module._is_secret_like_profile_env_name("OIDC_AUTHORIZATION_ENDPOINT") is False
    assert compose_module._is_secret_like_profile_env_name("PUBLIC_API_URL") is False
    assert compose_module._is_secret_like_profile_env_name("SLACK_WEBHOOK_URL") is True
    assert compose_module._is_secret_like_profile_env_name("DISCORD_WEBHOOK_URL") is True
    assert compose_module._is_secret_like_profile_env_name("SLACK_WEBHOOK_SECRET_URL") is True
    assert compose_module._is_secret_like_profile_env_name("PASSWORD_ENDPOINT") is True
    assert compose_module._is_secret_like_profile_env_name("PAYMENTS_CLIENT_SECRET_URI") is True
    assert compose_module._is_secret_like_profile_env_name("SERVICE_ACCESS_KEY_URL") is True

    assert compose_module._value_has_url_userinfo("https://user:pass@example.test/repo") is True
    assert (
        compose_module._value_has_url_userinfo("git remote https://user:pass@example.test/repo.git")
        is True
    )
    assert (
        compose_module._value_has_url_userinfo("https://example.test/callback?access_key=secret")
        is True
    )
    assert (
        compose_module._value_has_url_userinfo("https://example.test/callback?accessToken=secret")
        is True
    )
    assert (
        compose_module._value_has_url_userinfo("https://example.test/callback?refreshToken=secret")
        is True
    )
    assert (
        compose_module._value_has_url_userinfo("https://example.test/callback?authToken=secret")
        is True
    )
    assert (
        compose_module._value_has_url_userinfo("https://example.test/callback?ok=1;password=secret")
        is True
    )
    assert (
        compose_module._value_has_url_userinfo("https://example.test/callback#token=secret") is True
    )
    assert compose_module._value_has_url_userinfo("https://[::1") is False
    assert compose_module._value_has_url_userinfo("https://example.test/repo") is False


@pytest.mark.unit
def test_literal_profile_env_from_compose_carries_jwt_validation_config(
    tmp_path: Path,
) -> None:
    """JWT namespace config is carried, while exact raw JWT secrets are skipped."""

    assert compose_module._is_secret_like_profile_env_name("JWT") is True
    assert compose_module._is_secret_like_profile_env_name("JWT_SECRET") is True
    assert compose_module._is_secret_like_profile_env_name("JWT_ALGORITHM") is False
    assert compose_module._is_secret_like_profile_env_name("JWT_ISSUER") is False
    assert compose_module._is_secret_like_profile_env_name("JWT_AUDIENCE") is False

    compose_file = tmp_path / "missing-compose.yml"

    profile_env = dict(
        literal_profile_env_from_compose(
            compose_file,
            compose_env={
                "JWT": "header.payload.signature",
                "JWT_SECRET": "jwt-secret",
                "JWT_ALGORITHM": "RS256",
                "JWT_ISSUER": "https://issuer.example",
                "JWT_AUDIENCE": "awf-api",
            },
            worker_env={},
        )
    )

    assert "JWT" not in profile_env
    assert "JWT_SECRET" not in profile_env
    assert profile_env["JWT_ALGORITHM"] == "RS256"
    assert profile_env["JWT_ISSUER"] == "https://issuer.example"
    assert profile_env["JWT_AUDIENCE"] == "awf-api"


@pytest.mark.unit
def test_literal_profile_env_from_compose_redacts_camelcase_url_token_fields(
    tmp_path: Path,
) -> None:
    """Hosted profile_env skips URLs with camelCase credential parameters."""

    compose_file = tmp_path / "missing-compose.yml"

    profile_env = dict(
        literal_profile_env_from_compose(
            compose_file,
            compose_env={
                "CALLBACK_URL": "https://app.example/cb?accessToken=raw-access",
                "REFRESH_CALLBACK_URL": "https://app.example/cb?refreshToken=raw-refresh",
                "AUTH_CALLBACK_URL": "https://app.example/cb?authToken=raw-auth",
                "PUBLIC_CALLBACK_URL": "https://app.example/cb?state=public",
            },
            worker_env={},
        )
    )

    assert "CALLBACK_URL" not in profile_env
    assert "REFRESH_CALLBACK_URL" not in profile_env
    assert "AUTH_CALLBACK_URL" not in profile_env
    assert profile_env["PUBLIC_CALLBACK_URL"] == "https://app.example/cb?state=public"


@pytest.mark.unit
def test_literal_profile_env_from_compose_redacts_bare_jwt_url_fields(
    tmp_path: Path,
) -> None:
    """Hosted profile_env skips URLs with bare JWT query parameters."""

    compose_file = tmp_path / "missing-compose.yml"

    profile_env = dict(
        literal_profile_env_from_compose(
            compose_file,
            compose_env={
                "CALLBACK_URL": "https://app.example/cb?jwt=raw-jwt-token",
                "PUBLIC_CALLBACK_URL": "https://app.example/cb?state=public",
            },
            worker_env={},
        )
    )

    assert "CALLBACK_URL" not in profile_env
    assert profile_env["PUBLIC_CALLBACK_URL"] == "https://app.example/cb?state=public"


@pytest.mark.unit
def test_literal_profile_env_from_compose_redacts_session_url_fields(
    tmp_path: Path,
) -> None:
    """Hosted profile_env skips URLs with session query credentials."""

    compose_file = tmp_path / "missing-compose.yml"

    profile_env = dict(
        literal_profile_env_from_compose(
            compose_file,
            compose_env={
                "CALLBACK_URL": "https://app.example/cb?session=raw-session",
                "SESSION_CALLBACK_URL": "https://app.example/cb?sessionId=raw-session-id",
                "PUBLIC_CALLBACK_URL": "https://app.example/cb?state=public",
            },
            worker_env={},
        )
    )

    assert "CALLBACK_URL" not in profile_env
    assert "SESSION_CALLBACK_URL" not in profile_env
    assert profile_env["PUBLIC_CALLBACK_URL"] == "https://app.example/cb?state=public"


@pytest.mark.unit
def test_literal_profile_env_from_compose_redacts_secret_named_url_literals(
    tmp_path: Path,
) -> None:
    """Endpoint suffixes do not override explicit secret-name tokens."""

    compose_file = tmp_path / "missing-compose.yml"

    profile_env = dict(
        literal_profile_env_from_compose(
            compose_file,
            compose_env={
                "SLACK_WEBHOOK_URL": "https://hooks.slack.com/services/raw-slack-path-secret",
                "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/raw-discord-path-secret",
                "SLACK_WEBHOOK_SECRET_URL": "https://hooks.slack.com/services/raw-path-secret",
                "PASSWORD_ENDPOINT": "https://vault.example/passwords/raw-password-path",
                "PAYMENTS_CLIENT_SECRET_URI": "https://pay.example/setup/client-secret",
                "SERVICE_ACCESS_KEY_URL": "https://keys.example/service/raw-key",
                "AUTHORIZATION_URL": "https://issuer.example/oauth/authorize",
                "OIDC_TOKEN_URL": "https://issuer.example/oauth/token",
            },
            worker_env={},
        )
    )

    assert "SLACK_WEBHOOK_URL" not in profile_env
    assert "DISCORD_WEBHOOK_URL" not in profile_env
    assert "SLACK_WEBHOOK_SECRET_URL" not in profile_env
    assert "PASSWORD_ENDPOINT" not in profile_env
    assert "PAYMENTS_CLIENT_SECRET_URI" not in profile_env
    assert "SERVICE_ACCESS_KEY_URL" not in profile_env
    assert profile_env["AUTHORIZATION_URL"] == "https://issuer.example/oauth/authorize"
    assert profile_env["OIDC_TOKEN_URL"] == "https://issuer.example/oauth/token"
    blob = "\x00".join(profile_env.values())
    assert "raw-path-secret" not in blob
    assert "raw-slack-path-secret" not in blob
    assert "raw-discord-path-secret" not in blob
    assert "raw-password-path" not in blob
    assert "client-secret" not in blob
    assert "raw-key" not in blob


@pytest.mark.unit
def test_literal_profile_env_from_compose_redacts_equals_delimited_credential_blobs(
    tmp_path: Path,
) -> None:
    """Neutral env names do not carry dotenv/INI/npmrc credential assignments."""

    compose_file = tmp_path / "missing-compose.yml"

    profile_env = dict(
        literal_profile_env_from_compose(
            compose_file,
            compose_env={
                "APP_CONFIG": "client_secret=raw-client-secret",
                "NPMRC": "//registry.npmjs.org/:_authToken=raw-npm-token",
                "APP_JSON": '{"client_secret":"raw-json-secret"}',
                "APP_SETTINGS": "mode=hosted retries=3",
            },
            worker_env={},
        )
    )

    assert "APP_CONFIG" not in profile_env
    assert "NPMRC" not in profile_env
    assert "APP_JSON" not in profile_env
    assert profile_env["APP_SETTINGS"] == "mode=hosted retries=3"
    blob = "\x00".join(profile_env.values())
    assert "raw-client-secret" not in blob
    assert "raw-npm-token" not in blob
    assert "raw-json-secret" not in blob


@pytest.mark.unit
def test_literal_profile_env_from_compose_preserves_local_postgres_dsn_aliases(
    tmp_path: Path,
) -> None:
    """Passwordless local Postgres DSN aliases are app config, not hosted secrets."""

    compose_file = tmp_path / "missing-compose.yml"

    profile_env = dict(
        literal_profile_env_from_compose(
            compose_file,
            compose_env={
                "SQLALCHEMY_DATABASE_URI": "postgresql://awf@postgres:5432/app",
                "POSTGRES_URL": "postgres://postgres@postgres/app",
                "APP_DATABASE_URL": "postgresql+asyncpg://awf@postgres:5432/app",
                "EXTERNAL_POSTGRES_URL": "postgres://awf@db.example.test/app",
                "APP_POSTGRES_URL": "postgres://awf@postgres/app?access_token=raw-token",
                "POSTGRES_URL_WITH_PASSWORD": "postgres://awf:raw-password@postgres/app",
            },
            worker_env={},
        )
    )

    assert profile_env["SQLALCHEMY_DATABASE_URI"] == "postgresql://awf@postgres:5432/app"
    assert profile_env["POSTGRES_URL"] == "postgres://postgres@postgres/app"
    assert profile_env["APP_DATABASE_URL"] == "postgresql+asyncpg://awf@postgres:5432/app"
    assert "EXTERNAL_POSTGRES_URL" not in profile_env
    assert "APP_POSTGRES_URL" not in profile_env
    assert "POSTGRES_URL_WITH_PASSWORD" not in profile_env


@pytest.mark.unit
def test_name_only_credential_identifier_uses_passthrough_not_profile_env(
    tmp_path: Path,
) -> None:
    """Hosted resolves credential identifiers by name without direct env carry."""

    compose_file = tmp_path / "missing-compose.yml"
    compose_env = {
        "AWS_ACCESS_KEY_ID": "AKIA_PROFILE_IDENTIFIER",
        "OLLAMA_HOST": "http://ollama.profile:11434",
    }
    worker_env = {"AWS_ACCESS_KEY_ID": "AKIA_PROFILE_IDENTIFIER"}

    profile_env = dict(
        literal_profile_env_from_compose(
            compose_file,
            compose_env=compose_env,
            worker_env=worker_env,
        )
    )
    names = filter_hosted_env_passthrough_names(
        ("AWS_ACCESS_KEY_ID", "OLLAMA_HOST"),
        compose_file=compose_file,
        compose_env=compose_env,
        worker_env=worker_env,
    )

    assert "AWS_ACCESS_KEY_ID" not in profile_env
    assert profile_env["OLLAMA_HOST"] == "http://ollama.profile:11434"
    assert names == ("AWS_ACCESS_KEY_ID",)


@pytest.mark.unit
def test_name_only_credential_identifier_passthrough_slot_stays_passthrough(
    tmp_path: Path,
) -> None:
    """Compose pass-through credential names are not converted to profile env."""

    compose_file = tmp_path / "missing-compose.yml"
    compose_env = {
        "AWS_ACCESS_KEY_ID": compose_module._COMPOSE_PASSTHROUGH,  # noqa: SLF001
        "OLLAMA_HOST": "http://ollama.profile:11434",
    }

    profile_env = dict(
        literal_profile_env_from_compose(
            compose_file,
            compose_env=compose_env,
            worker_env={"AWS_ACCESS_KEY_ID": "AKIA_WORKER_IDENTIFIER"},
        )
    )
    names = filter_hosted_env_passthrough_names(
        ("AWS_ACCESS_KEY_ID", "OLLAMA_HOST"),
        compose_file=compose_file,
        compose_env=compose_env,
        worker_env={"AWS_ACCESS_KEY_ID": "AKIA_WORKER_IDENTIFIER"},
    )

    assert "AWS_ACCESS_KEY_ID" not in profile_env
    assert profile_env["OLLAMA_HOST"] == "http://ollama.profile:11434"
    assert names == ("AWS_ACCESS_KEY_ID",)


@pytest.mark.unit
def test_literal_profile_env_from_compose_preserves_authorization_endpoints(
    tmp_path: Path,
) -> None:
    """Hosted profile_env carries non-secret auth endpoint config."""

    compose_file = tmp_path / "missing-compose.yml"
    profile_env = dict(
        literal_profile_env_from_compose(
            compose_file,
            compose_env={
                "AUTHORIZATION": "Digest profile-auth-header",
                "HTTP_AUTHORIZATION": "Negotiate profile-auth-header",
                "AUTHORIZATION_URL": "https://issuer.example/oauth/authorize",
                "OIDC_AUTHORIZATION_ENDPOINT": "https://issuer.example/oauth2/v1/authorize",
                "UPSTREAM_AUTH": "Bearer upstream-token",
                "OLLAMA_HOST": "http://ollama.profile:11434",
            },
            worker_env={},
        )
    )

    assert "AUTHORIZATION" not in profile_env
    assert "HTTP_AUTHORIZATION" not in profile_env
    assert "UPSTREAM_AUTH" not in profile_env
    assert profile_env["AUTHORIZATION_URL"] == "https://issuer.example/oauth/authorize"
    assert (
        profile_env["OIDC_AUTHORIZATION_ENDPOINT"] == "https://issuer.example/oauth2/v1/authorize"
    )
    assert profile_env["OLLAMA_HOST"] == "http://ollama.profile:11434"


@pytest.mark.unit
@pytest.mark.parametrize(
    "headers",
    [
        '{"Authorization":"Bearer quoted-bearer-token"}',
        "{'Authorization':'Basic quoted-basic-token'}",
    ],
)
def test_literal_profile_env_from_compose_redacts_quoted_authorization_header_values(
    tmp_path: Path,
    headers: str,
) -> None:
    """Hosted profile_env does not carry auth headers hidden in JSON-like values."""

    compose_file = tmp_path / "missing-compose.yml"

    profile_env = dict(
        literal_profile_env_from_compose(
            compose_file,
            compose_env={
                "REQUEST_HEADERS": headers,
                "OLLAMA_HOST": "http://ollama.profile:11434",
            },
            worker_env={},
        )
    )

    assert "REQUEST_HEADERS" not in profile_env
    assert profile_env["OLLAMA_HOST"] == "http://ollama.profile:11434"


@pytest.mark.unit
def test_literal_profile_env_from_compose_redacts_api_key_header_values(
    tmp_path: Path,
) -> None:
    """Hosted profile_env does not carry API-key headers hidden in generic values."""

    compose_file = tmp_path / "missing-compose.yml"

    profile_env = dict(
        literal_profile_env_from_compose(
            compose_file,
            compose_env={
                "REQUEST_HEADERS": '{"X-Api-Key":"sk_profile_header_secret"}',
                "CURL_ARGS": '-fsS -H "x-api-key: sk_profile_curl_secret"',
                "VENDOR_REQUEST_HEADERS": ('{"x-goog-api-key":"sk_profile_vendor_header_secret"}'),
                "VENDOR_CURL_ARGS": ("-fsS -H 'x-goog-api-key: sk_profile_vendor_curl_secret'"),
                "OLLAMA_HOST": "http://ollama.profile:11434",
            },
            worker_env={},
        )
    )

    assert "REQUEST_HEADERS" not in profile_env
    assert "CURL_ARGS" not in profile_env
    assert "VENDOR_REQUEST_HEADERS" not in profile_env
    assert "VENDOR_CURL_ARGS" not in profile_env
    assert profile_env["OLLAMA_HOST"] == "http://ollama.profile:11434"
    blob = "\x00".join(profile_env.values())
    assert "sk_profile_header_secret" not in blob
    assert "sk_profile_curl_secret" not in blob
    assert "sk_profile_vendor_header_secret" not in blob
    assert "sk_profile_vendor_curl_secret" not in blob


@pytest.mark.unit
def test_literal_profile_env_from_compose_redacts_auth_token_header_values(
    tmp_path: Path,
) -> None:
    """Hosted profile_env does not carry token headers hidden in generic values."""

    compose_file = tmp_path / "missing-compose.yml"

    profile_env = dict(
        literal_profile_env_from_compose(
            compose_file,
            compose_env={
                "REQUEST_HEADERS": '{"X-Auth-Token":"profile-header-token"}',
                "CURL_ARGS": "-fsS -H 'X-Auth-Token: profile-curl-token'",
                "OLLAMA_HOST": "http://ollama.profile:11434",
            },
            worker_env={},
        )
    )

    assert "REQUEST_HEADERS" not in profile_env
    assert "CURL_ARGS" not in profile_env
    assert profile_env["OLLAMA_HOST"] == "http://ollama.profile:11434"
    blob = "\x00".join(profile_env.values())
    assert "profile-header-token" not in blob
    assert "profile-curl-token" not in blob


@pytest.mark.unit
def test_literal_profile_env_from_compose_redacts_security_token_header_values(
    tmp_path: Path,
) -> None:
    """Hosted profile_env does not carry AWS security-token headers in generic values."""

    compose_file = tmp_path / "missing-compose.yml"

    profile_env = dict(
        literal_profile_env_from_compose(
            compose_file,
            compose_env={
                "REQUEST_HEADERS": '{"X-Amz-Security-Token":"profile-header-session-token"}',
                "CURL_ARGS": "-fsS -H 'X-Amz-Security-Token: profile-curl-session-token'",
                "OLLAMA_HOST": "http://ollama.profile:11434",
            },
            worker_env={},
        )
    )

    assert "REQUEST_HEADERS" not in profile_env
    assert "CURL_ARGS" not in profile_env
    assert profile_env["OLLAMA_HOST"] == "http://ollama.profile:11434"
    blob = "\x00".join(profile_env.values())
    assert "profile-header-session-token" not in blob
    assert "profile-curl-session-token" not in blob


@pytest.mark.unit
def test_literal_profile_env_from_compose_redacts_gitlab_token_header_values(
    tmp_path: Path,
) -> None:
    """Hosted profile_env does not carry GitLab token headers hidden in generic values."""

    compose_file = tmp_path / "missing-compose.yml"

    profile_env = dict(
        literal_profile_env_from_compose(
            compose_file,
            compose_env={
                "REQUEST_HEADERS": '{"PRIVATE-TOKEN":"glpat_profile_header_secret"}',
                "CURL_ARGS": "-fsS -H 'JOB-TOKEN: profile-curl-job-secret'",
                "OLLAMA_HOST": "http://ollama.profile:11434",
            },
            worker_env={},
        )
    )

    assert "REQUEST_HEADERS" not in profile_env
    assert "CURL_ARGS" not in profile_env
    assert profile_env["OLLAMA_HOST"] == "http://ollama.profile:11434"
    blob = "\x00".join(profile_env.values())
    assert "glpat_profile_header_secret" not in blob
    assert "profile-curl-job-secret" not in blob


@pytest.mark.unit
def test_literal_profile_env_from_compose_redacts_cookie_header_values(
    tmp_path: Path,
) -> None:
    """Hosted profile_env does not carry Cookie headers hidden in generic values."""

    compose_file = tmp_path / "missing-compose.yml"

    profile_env = dict(
        literal_profile_env_from_compose(
            compose_file,
            compose_env={
                "REQUEST_HEADERS": "Cookie: session=profile-cookie-secret",
                "CURL_ARGS": "-fsS -H 'Cookie: session=profile-curl-cookie-secret'",
                "OLLAMA_HOST": "http://ollama.profile:11434",
            },
            worker_env={},
        )
    )

    assert "REQUEST_HEADERS" not in profile_env
    assert "CURL_ARGS" not in profile_env
    assert profile_env["OLLAMA_HOST"] == "http://ollama.profile:11434"
    blob = "\x00".join(profile_env.values())
    assert "profile-cookie-secret" not in blob
    assert "profile-curl-cookie-secret" not in blob


@pytest.mark.unit
def test_literal_profile_env_from_compose_redacts_neutral_config_blob_credentials(
    tmp_path: Path,
) -> None:
    """Hosted profile_env skips common credential fields in neutral config blobs."""

    compose_file = tmp_path / "missing-compose.yml"

    profile_env = dict(
        literal_profile_env_from_compose(
            compose_file,
            compose_env={
                "OAUTH_CONFIG": '{"client_secret":"profile-oauth-client-secret"}',
                "APP_CONFIG": "{'password':'profile-app-password'}",
                "SDK_CONFIG": '{"apiKey":"profile-sdk-api-key"}',
                "SAFE_CONFIG": '{"issuer":"https://issuer.example","timeout":30}',
                "OLLAMA_HOST": "http://ollama.profile:11434",
            },
            worker_env={},
        )
    )

    assert "OAUTH_CONFIG" not in profile_env
    assert "APP_CONFIG" not in profile_env
    assert "SDK_CONFIG" not in profile_env
    assert profile_env["SAFE_CONFIG"] == '{"issuer":"https://issuer.example","timeout":30}'
    assert profile_env["OLLAMA_HOST"] == "http://ollama.profile:11434"
    blob = "\x00".join(profile_env.values())
    assert "profile-oauth-client-secret" not in blob
    assert "profile-app-password" not in blob
    assert "profile-sdk-api-key" not in blob


@pytest.mark.unit
def test_hosted_git_config_filters_unsafe_entries_and_reindexes_safe_ones() -> None:
    """Hosted git config keeps only safe literal and worker-resolved entries."""

    profile_env, aliases = compose_module._hosted_git_config_env(
        {
            "GIT_CONFIG_COUNT": "8",
            # Missing value -> skipped.
            "GIT_CONFIG_KEY_0": "user.name",
            # Worker-resolved key -> skipped.
            "GIT_CONFIG_KEY_1": "${GIT_CONFIG_KEY_SOURCE}",
            "GIT_CONFIG_VALUE_1": "ignored",
            # Credential-bearing key URL -> skipped.
            "GIT_CONFIG_KEY_2": "url.https://user:pass@example.test/.insteadOf",
            "GIT_CONFIG_VALUE_2": "https://example.test/",
            # Bitbucket agent rewrite -> skipped when mount-backed askpass owns it.
            "GIT_CONFIG_KEY_3": compose_module._BITBUCKET_AGENT_INSTEADOF_KEY,
            "GIT_CONFIG_VALUE_3": "https://bitbucket.org/",
            # Credential-bearing literal value -> skipped.
            "GIT_CONFIG_KEY_4": "credential.helper",
            "GIT_CONFIG_VALUE_4": "https://user:pass@example.test/helper",
            # Bearer-token literal value -> skipped.
            "GIT_CONFIG_KEY_5": "credential.helper",
            "GIT_CONFIG_VALUE_5": "Authorization: Bearer bearerToken123456",
            # Safe literal value -> carried and reindexed to slot 0.
            "GIT_CONFIG_KEY_6": "user.email",
            "GIT_CONFIG_VALUE_6": "mona@example.test",
            # Safe worker-resolved value -> alias and reindexed to slot 1.
            "GIT_CONFIG_KEY_7": "user.name",
            "GIT_CONFIG_VALUE_7": "${GIT_AUTHOR_NAME}",
        },
        worker_env={
            "GIT_CONFIG_KEY_SOURCE": "user.name",
            "GIT_AUTHOR_NAME": "Mona",
        },
        skip_bitbucket_agent_rewrites=True,
    )

    assert profile_env == (
        ("GIT_CONFIG_KEY_0", "user.email"),
        ("GIT_CONFIG_VALUE_0", "mona@example.test"),
        ("GIT_CONFIG_KEY_1", "user.name"),
        ("GIT_CONFIG_COUNT", "2"),
    )
    assert aliases == (("GIT_CONFIG_VALUE_1", "GIT_AUTHOR_NAME"),)


@pytest.mark.unit
def test_hosted_git_config_preserves_safe_git_ssh_rewrite_key() -> None:
    """Hosted git config keeps passwordless git+ssh rewrite keys."""

    profile_env, aliases = compose_module._hosted_git_config_env(
        {
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "url.git+ssh://git@github.com/.insteadOf",
            "GIT_CONFIG_VALUE_0": "https://github.com/",
            "GIT_CONFIG_KEY_1": "url.git+ssh://token@github.com/.insteadOf",
            "GIT_CONFIG_VALUE_1": "https://github.com/",
        },
        worker_env={},
        skip_bitbucket_agent_rewrites=False,
    )

    assert profile_env == (
        ("GIT_CONFIG_KEY_0", "url.git+ssh://git@github.com/.insteadOf"),
        ("GIT_CONFIG_VALUE_0", "https://github.com/"),
        ("GIT_CONFIG_COUNT", "1"),
    )
    assert aliases == ()


@pytest.mark.unit
def test_hosted_git_config_ignores_unusable_count_values() -> None:
    """Nonliteral and noninteger git-config counts produce no hosted block."""

    assert compose_module._hosted_git_config_env(
        {"GIT_CONFIG_COUNT": "${GIT_CONFIG_COUNT}"},
        worker_env={"GIT_CONFIG_COUNT": "1"},
        skip_bitbucket_agent_rewrites=False,
    ) == ((), ())
    assert compose_module._hosted_git_config_env(
        {"GIT_CONFIG_COUNT": "not-an-int"},
        worker_env={},
        skip_bitbucket_agent_rewrites=False,
    ) == ((), ())
