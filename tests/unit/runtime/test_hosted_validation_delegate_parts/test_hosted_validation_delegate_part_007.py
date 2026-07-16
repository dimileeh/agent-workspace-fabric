"""Hosted validation profile postgres-trust and docker-mode edge tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from awf.profiles.models import WorkspaceProfile
from awf.runtime.hosted_delegation import _hosted_validation_profile_payload
from awf.runtime.hosted_delegation_payloads import _hosted_validation_attach_rendered_stack


@pytest.mark.unit
def test_hosted_profile_env_omits_credentials_passwordless_postgres_and_trust() -> None:
    """Cloud rejects profile ${NAME} DB/password stubs; omit, rewrite, trust."""
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-profile-env-contract",
            "runtime": {
                "environment": {
                    "OLLAMA_HOST": "http://ollama.profile:11434",
                    "NPM_TOKEN": "npm-profile-secret",
                    "DATABASE_URL": (
                        "postgresql+asyncpg://awf:literal-db-secret@postgres:5432/awf"
                    ),
                    "APP_DSN": "${DATABASE_URL}",
                    "DB_URL": "${DB_URL}",
                    "PUBLIC_HEADER": "Bearer ${POSTGRES_PASSWORD}",
                    "DB_HEADER": "Bearer ${DB_URL}",
                }
            },
            "services": [
                {
                    "name": "postgres",
                    "image": "postgres:16",
                    "environment": {
                        "POSTGRES_USER": "awf",
                        "POSTGRES_DB": "awf",
                        "POSTGRES_PASSWORD": "literal-service-password",
                        "EXTERNAL_API_KEY": "${SERVICE_API_KEY}",
                    },
                },
                {
                    "name": "redis",
                    "image": "redis:7",
                    "environment": {
                        "REDIS_URL": "redis://cache:6379/0",
                        "CACHE_TOKEN": "literal-cache-token",
                    },
                },
            ],
        }
    )

    payload = _hosted_validation_profile_payload(profile)

    assert payload["runtime"]["environment"] == {
        "OLLAMA_HOST": "http://ollama.profile:11434",
        "DATABASE_URL": "postgresql+asyncpg://awf@postgres:5432/awf",
    }
    assert payload["services"][0]["environment"] == {
        "POSTGRES_USER": "awf",
        "POSTGRES_DB": "awf",
        "POSTGRES_HOST_AUTH_METHOD": "trust",
    }
    assert payload["services"][1]["environment"] == {
        "REDIS_URL": "redis://cache:6379/0",
    }
    body = json.dumps(payload, sort_keys=True)
    assert "literal-db-secret" not in body
    assert "literal-service-password" not in body
    assert "literal-cache-token" not in body
    assert "${DATABASE_URL}" not in body
    assert "${DB_URL}" not in body
    assert "${POSTGRES_PASSWORD}" not in body
    assert "POSTGRES_PASSWORD" not in body
    assert "NPM_TOKEN" not in body
    assert "EXTERNAL_API_KEY" not in body
    assert "CACHE_TOKEN" not in body
    assert "DB_URL" not in body
    assert "DB_HEADER" not in body
    assert "PUBLIC_HEADER" not in body
    assert "APP_DSN" not in body


@pytest.mark.unit
def test_hosted_profile_docker_mode_none_becomes_compose_with_sidecars(
    tmp_path: Path,
) -> None:
    """Non-empty rendered stack + profile services: hosted none → compose."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  postgres:
    image: postgres:16
    environment:
      POSTGRES_USER: awf
""".lstrip(),
        encoding="utf-8",
    )
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-mode-none-sidecars",
            "docker": {"mode": "none"},
            "services": [{"name": "postgres", "image": "postgres:16"}],
        }
    )
    payload: dict[str, Any] = {
        "profile": _hosted_validation_profile_payload(profile),
    }

    _hosted_validation_attach_rendered_stack(
        payload,
        compose_project="awf_ws_hosted",
        compose_file=compose_file,
        omit_credential_env_keys=True,
    )

    assert payload["profile"]["docker"]["mode"] == "compose"
    assert "postgres" in payload["rendered_stack"]["services"]


@pytest.mark.unit
def test_hosted_profile_docker_mode_none_becomes_compose_for_rendered_only_sidecars(
    tmp_path: Path,
) -> None:
    """Companion sidecars live in the rendered stack, not profile.services."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  postgres:
    image: postgres:16
""".lstrip(),
        encoding="utf-8",
    )
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-mode-none-companions",
            "docker": {"mode": "none"},
            "services": [],
        }
    )
    payload: dict[str, Any] = {
        "profile": _hosted_validation_profile_payload(profile),
    }

    _hosted_validation_attach_rendered_stack(
        payload,
        compose_project="awf_ws_hosted",
        compose_file=compose_file,
        omit_credential_env_keys=True,
    )

    assert payload["profile"]["docker"]["mode"] == "compose"
    assert "postgres" in payload["rendered_stack"]["services"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("docker_mode", "profile_services", "compose_body", "expected_mode"),
    [
        (
            "dind",
            [{"name": "postgres", "image": "postgres:16"}],
            "services:\n  postgres:\n    image: postgres:16\n",
            "dind",
        ),
        (
            "none",
            [{"name": "postgres", "image": "postgres:16"}],
            "services:\n  agent:\n    image: awf-agent:latest\n",
            "none",
        ),
    ],
)
def test_hosted_profile_docker_mode_none_not_converted_without_sidecars(
    tmp_path: Path,
    docker_mode: str,
    profile_services: list[dict[str, str]],
    compose_body: str,
    expected_mode: str,
) -> None:
    """Never convert dind or stacks with no non-agent rendered services."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(compose_body, encoding="utf-8")
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-mode-guard",
            "docker": {"mode": docker_mode},
            "services": profile_services,
        }
    )
    payload: dict[str, Any] = {
        "profile": _hosted_validation_profile_payload(profile),
    }

    _hosted_validation_attach_rendered_stack(
        payload,
        compose_project="awf_ws_hosted",
        compose_file=compose_file,
        omit_credential_env_keys=True,
    )

    assert payload["profile"]["docker"]["mode"] == expected_mode


@pytest.mark.unit
def test_hosted_profile_passwordless_postgres_url_edges() -> None:
    """Only Postgres URLs with a password arm are rewritten."""
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-profile-pg-url-edges",
            "runtime": {
                "environment": {
                    "ALREADY_OPEN": "postgresql://awf@postgres:5432/awf",
                    "STRIPPED_USERLESS": ("postgresql://:literal-only-secret@postgres:5432/awf"),
                    "HTTPS_URL": "https://user:not-postgres@example.test/db",
                    "SAFE_HOST": "http://ollama:11434",
                }
            },
        }
    )

    payload = _hosted_validation_profile_payload(profile)

    assert payload["runtime"]["environment"] == {
        "ALREADY_OPEN": "postgresql://awf@postgres:5432/awf",
        "STRIPPED_USERLESS": "postgresql://postgres:5432/awf",
        "HTTPS_URL": "${HTTPS_URL}",
        "SAFE_HOST": "http://ollama:11434",
    }
    body = json.dumps(payload, sort_keys=True)
    assert "literal-only-secret" not in body
    assert "not-postgres" not in body


@pytest.mark.unit
@pytest.mark.parametrize(
    ("database_url", "secret"),
    [
        (
            "postgresql://awf:userinfo-pw@postgres:5432/awf?sslpassword=query-pg-secret",
            "query-pg-secret",
        ),
        (
            "postgresql+asyncpg://awf@postgres:5432/awf#password=fragment-pg-secret",
            "fragment-pg-secret",
        ),
        (
            "postgres://postgres:5432/awf?sslpassword=host-only-query-secret",
            "host-only-query-secret",
        ),
        # Path / matrix-style credential fields must omit even when userinfo is kept.
        (
            "postgresql://awf@postgres/db;password=rawsecret",
            "rawsecret",
        ),
        (
            "postgresql://awf@postgres/db%3Bpassword%3Drawsecret",
            "rawsecret",
        ),
        (
            "postgresql+asyncpg://awf@postgres:5432/awf;sslpassword=path-pg-secret",
            "path-pg-secret",
        ),
        # Nested basic-auth URLs in path/fragment must omit even with userinfo
        # exemption on the Postgres authority (include_url_userinfo=False).
        (
            "postgresql://postgres@postgres/db/https://user:nested-path-pw@svc",
            "nested-path-pw",
        ),
        (
            "postgresql://postgres@postgres/db/https%3A%2F%2Fuser%3Anested-enc-pw%40svc",
            "nested-enc-pw",
        ),
        (
            "postgresql://awf@postgres:5432/awf#https%3A%2F%2Fuser%3Anested-frag-pw%40svc",
            "nested-frag-pw",
        ),
    ],
)
def test_hosted_profile_passwordless_postgres_omits_query_fragment_credentials(
    database_url: str,
    secret: str,
) -> None:
    """Passwordless Postgres rewrite must not preserve path/query/fragment secrets."""
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-profile-pg-url-query-creds",
            "runtime": {
                "environment": {
                    "DATABASE_URL": database_url,
                    "KEEP_OPEN": "postgresql://awf@postgres:5432/awf?sslmode=require",
                    "OLLAMA_HOST": "http://ollama.profile:11434",
                }
            },
        }
    )

    payload = _hosted_validation_profile_payload(profile)

    assert payload["runtime"]["environment"] == {
        "KEEP_OPEN": "postgresql://awf@postgres:5432/awf?sslmode=require",
        "OLLAMA_HOST": "http://ollama.profile:11434",
    }
    body = json.dumps(payload, sort_keys=True)
    assert secret not in body
    assert "userinfo-pw" not in body
    assert "DATABASE_URL" not in body
    assert "${DATABASE_URL}" not in body


@pytest.mark.unit
@pytest.mark.parametrize(
    ("database_url", "rewritten", "leak"),
    [
        (
            "postgresql://${POSTGRES_PASSWORD}@postgres:5432/awf",
            "postgresql://postgres:5432/awf",
            "${POSTGRES_PASSWORD}",
        ),
        (
            "postgresql+asyncpg://%24%7BPOSTGRES_PASSWORD%7D@postgres:5432/awf",
            "postgresql+asyncpg://postgres:5432/awf",
            "POSTGRES_PASSWORD",
        ),
        (
            "postgresql://ghp_exampleusernameonlytoken12@postgres:5432/awf",
            "postgresql://postgres:5432/awf",
            "ghp_exampleusernameonlytoken12",
        ),
        (
            "postgres://${DATABASE_URL}@postgres:5432/awf",
            "postgres://postgres:5432/awf",
            "${DATABASE_URL}",
        ),
        (
            "postgresql://env%3A%2F%2Fpg_secret_ref@postgres:5432/awf",
            "postgresql://postgres:5432/awf",
            "env://pg_secret_ref",
        ),
        # Password arm present: strip password AND drop credential usernames.
        (
            "postgresql://${POSTGRES_PASSWORD}:x@postgres:5432/awf",
            "postgresql://postgres:5432/awf",
            "${POSTGRES_PASSWORD}",
        ),
        (
            "postgresql://ghp_examplepasswordarmtoken12:x@postgres:5432/awf",
            "postgresql://postgres:5432/awf",
            "ghp_examplepasswordarmtoken12",
        ),
        (
            "postgresql+asyncpg://%24%7BPOSTGRES_PASSWORD%7D:lit-pw@postgres:5432/awf",
            "postgresql+asyncpg://postgres:5432/awf",
            "POSTGRES_PASSWORD",
        ),
    ],
)
def test_hosted_profile_passwordless_postgres_strips_username_only_credentials(
    database_url: str,
    rewritten: str,
    leak: str,
) -> None:
    """Credential usernames must be stripped even when a password arm is rewritten."""
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-profile-pg-url-userinfo-creds",
            "runtime": {
                "environment": {
                    "DATABASE_URL": database_url,
                    "KEEP_OPEN": "postgresql://awf@postgres:5432/awf",
                    "OLLAMA_HOST": "http://ollama.profile:11434",
                }
            },
        }
    )

    payload = _hosted_validation_profile_payload(profile)

    assert payload["runtime"]["environment"] == {
        "DATABASE_URL": rewritten,
        "KEEP_OPEN": "postgresql://awf@postgres:5432/awf",
        "OLLAMA_HOST": "http://ollama.profile:11434",
    }
    body = json.dumps(payload, sort_keys=True)
    assert leak not in body


@pytest.mark.unit
@pytest.mark.parametrize(
    ("database_url", "leak"),
    [
        (
            "postgresql://postgres@postgres/db/${API_TOKEN}",
            "${API_TOKEN}",
        ),
        (
            "postgresql://awf@postgres:5432/awf?options=${API_TOKEN}",
            "${API_TOKEN}",
        ),
        (
            "postgresql+asyncpg://awf@postgres:5432/awf#note=${DATABASE_URL}",
            "${DATABASE_URL}",
        ),
        (
            "postgres://postgres:5432/awf/${POSTGRES_PASSWORD}",
            "${POSTGRES_PASSWORD}",
        ),
    ],
)
def test_hosted_profile_passwordless_postgres_omits_credential_source_interpolations(
    database_url: str,
    leak: str,
) -> None:
    """Passwordless Postgres keep-path must still omit credential-source refs."""
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-profile-pg-url-cred-interp",
            "runtime": {
                "environment": {
                    "DATABASE_URL": database_url,
                    "KEEP_OPEN": "postgresql://awf@postgres:5432/awf?sslmode=require",
                    "OLLAMA_HOST": "http://ollama.profile:11434",
                }
            },
        }
    )

    payload = _hosted_validation_profile_payload(profile)

    assert payload["runtime"]["environment"] == {
        "KEEP_OPEN": "postgresql://awf@postgres:5432/awf?sslmode=require",
        "OLLAMA_HOST": "http://ollama.profile:11434",
    }
    body = json.dumps(payload, sort_keys=True)
    assert leak not in body
    assert "DATABASE_URL" not in body
    assert "${DATABASE_URL}" not in body


@pytest.mark.unit
@pytest.mark.parametrize(
    ("database_url", "secret"),
    [
        (
            "postgresql://postgres@postgres/db/sk-proj-abcdefghij123456",
            "sk-proj-abcdefghij123456",
        ),
        (
            "postgresql://awf@postgres:5432/awf?note=sk-proj-abcdefghij123456",
            "sk-proj-abcdefghij123456",
        ),
        (
            "postgresql+asyncpg://awf@postgres:5432/awf#sk-proj-abcdefghij123456",
            "sk-proj-abcdefghij123456",
        ),
    ],
)
def test_hosted_profile_passwordless_postgres_omits_raw_secret_tokens(
    database_url: str,
    secret: str,
) -> None:
    """Passwordless keep-path must still run the secret-token scan before accept."""
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-profile-pg-url-raw-token",
            "runtime": {
                "environment": {
                    "DATABASE_URL": database_url,
                    "KEEP_OPEN": "postgresql://awf@postgres:5432/awf?sslmode=require",
                    "OLLAMA_HOST": "http://ollama.profile:11434",
                }
            },
        }
    )

    payload = _hosted_validation_profile_payload(profile)

    assert payload["runtime"]["environment"] == {
        "KEEP_OPEN": "postgresql://awf@postgres:5432/awf?sslmode=require",
        "OLLAMA_HOST": "http://ollama.profile:11434",
    }
    body = json.dumps(payload, sort_keys=True)
    assert secret not in body
    assert "DATABASE_URL" not in body
    assert "${DATABASE_URL}" not in body


@pytest.mark.unit
@pytest.mark.parametrize(
    ("database_url", "leak"),
    [
        (
            "postgresql://postgres@postgres/db?sslrootcert=plain-file:///home/user/.pgcert",
            "plain-file:///home/user/.pgcert",
        ),
        (
            # Encoded :// must still be rejected; raw pattern scan alone misses it.
            "postgresql://postgres@postgres/db?sslrootcert=plain-file%3A%2F%2F%2Fhome%2Fuser%2F.pgcert",
            "plain-file%3A%2F%2F%2Fhome%2Fuser%2F.pgcert",
        ),
        (
            "postgresql://awf@postgres:5432/awf?note=env://PGPASSWORD",
            "env://PGPASSWORD",
        ),
        (
            "postgresql://awf@postgres:5432/awf?note=env%3A%2F%2FPGPASSWORD",
            "env%3A%2F%2FPGPASSWORD",
        ),
        (
            "postgresql+asyncpg://awf@postgres:5432/awf#keyring://codex/default",
            "keyring://codex/default",
        ),
        (
            "postgresql+asyncpg://awf@postgres:5432/awf#keyring%3A%2F%2Fcodex%2Fdefault",
            "keyring%3A%2F%2Fcodex%2Fdefault",
        ),
    ],
)
def test_hosted_profile_passwordless_postgres_omits_provider_refs(
    database_url: str,
    leak: str,
) -> None:
    """Passwordless keep-path must still reject provider credential refs."""
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-profile-pg-url-provider-ref",
            "runtime": {
                "environment": {
                    "DATABASE_URL": database_url,
                    "KEEP_OPEN": "postgresql://awf@postgres:5432/awf?sslmode=require",
                    "OLLAMA_HOST": "http://ollama.profile:11434",
                }
            },
        }
    )

    payload = _hosted_validation_profile_payload(profile)

    assert payload["runtime"]["environment"] == {
        "KEEP_OPEN": "postgresql://awf@postgres:5432/awf?sslmode=require",
        "OLLAMA_HOST": "http://ollama.profile:11434",
    }
    body = json.dumps(payload, sort_keys=True)
    assert leak not in body
    assert "DATABASE_URL" not in body
    assert "${DATABASE_URL}" not in body
