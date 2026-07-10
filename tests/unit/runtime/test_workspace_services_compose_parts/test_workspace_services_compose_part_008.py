"""No-Docker compose coverage for profile-declared workspace services (part 8)."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

import pytest
import yaml

from awf.profiles.compose import literal_profile_env_from_compose
from awf.profiles.compose_postgres_env import compose_postgres_service_hostnames
from awf.profiles.compose_profile_env import _local_postgres_database_url_without_tracked_password


@pytest.mark.unit
@pytest.mark.parametrize(
    ("payload", "expected_hostnames"),
    [
        ([], frozenset()),
        ({"services": []}, frozenset()),
        (
            {
                "services": {
                    123: {"image": "postgres:16-alpine"},
                    "not-a-service": [],
                    "db": {"image": "postgres:16-alpine"},
                }
            },
            frozenset({"db"}),
        ),
    ],
)
def test_compose_postgres_service_hostnames_tolerates_malformed_compose_shapes(
    tmp_path: Path,
    payload: object,
    expected_hostnames: frozenset[str],
) -> None:
    """Malformed Compose fragments do not create false local DB hostnames."""

    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(yaml.safe_dump(payload), encoding="utf-8")

    assert compose_postgres_service_hostnames(compose_file) == expected_hostnames


@pytest.mark.unit
def test_literal_profile_env_from_compose_preserves_passwordless_local_postgres_scheme(
    tmp_path: Path,
) -> None:
    """The common local ``postgres://user@postgres`` DSN is safe to replay."""

    compose_file = tmp_path / "compose.yml"
    database_url = "postgres://postgres@postgres:5432/app"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            "DATABASE_URL": database_url,
                            "OLLAMA_HOST": "http://ollama.profile:11434",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    profile_env = dict(literal_profile_env_from_compose(compose_file, worker_env={}))

    assert profile_env["DATABASE_URL"] == database_url
    assert profile_env["OLLAMA_HOST"] == "http://ollama.profile:11434"


@pytest.mark.unit
def test_literal_profile_env_from_compose_preserves_passwordless_local_postgres_url_with_tracked_password(
    tmp_path: Path,
) -> None:
    """A separate tracked Postgres password does not make a passwordless URL secret."""

    compose_file = tmp_path / "compose.yml"
    database_url = "postgresql://awf@postgres:5432/app"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "postgres": {
                        "image": "postgres:16-alpine",
                        "environment": {"POSTGRES_PASSWORD": "tracked-secret"},
                    },
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            "DATABASE_URL": database_url,
                            "OLLAMA_HOST": "http://ollama.profile:11434",
                        },
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    profile_env = dict(literal_profile_env_from_compose(compose_file, worker_env={}))

    assert profile_env["DATABASE_URL"] == database_url
    assert profile_env["OLLAMA_HOST"] == "http://ollama.profile:11434"
    assert "tracked-secret" not in "\x00".join(profile_env.values())


@pytest.mark.unit
def test_literal_profile_env_from_compose_preserves_custom_postgres_service_hostnames(
    tmp_path: Path,
) -> None:
    """Passwordless DB URLs may point at custom local Postgres service names."""

    compose_file = tmp_path / "compose.yml"
    database_url = "postgresql://awf@db:5432/app"
    awf_database_url = "postgresql+asyncpg://awf@database:5432/app"
    redis_database_url = "postgresql://awf@redis:5432/app"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "db": {"image": "registry.local:5000/postgres:16-alpine"},
                    "database": {
                        "image": "example/database-wrapper:latest",
                        "environment": {"POSTGRES_DB": "app"},
                    },
                    "redis": {"image": "redis:7-alpine"},
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            "DATABASE_URL": database_url,
                            "AWF_DATABASE_URL": awf_database_url,
                            "AWF_TEST_DATABASE_URL": redis_database_url,
                            "OLLAMA_HOST": "http://ollama.profile:11434",
                        },
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    profile_env = dict(literal_profile_env_from_compose(compose_file, worker_env={}))

    assert profile_env["DATABASE_URL"] == database_url
    assert profile_env["AWF_DATABASE_URL"] == awf_database_url
    assert "AWF_TEST_DATABASE_URL" not in profile_env
    assert profile_env["OLLAMA_HOST"] == "http://ollama.profile:11434"


@pytest.mark.unit
def test_literal_profile_env_from_compose_detects_custom_postgres_env_file_with_worker_env(
    tmp_path: Path,
) -> None:
    """Required env-file interpolation should not hide local Postgres services."""

    compose_file = tmp_path / "compose.yml"
    env_file = tmp_path / "postgres.env"
    env_file.write_text("POSTGRES_PASSWORD=${PGPW:?required}\n", encoding="utf-8")
    database_url = "postgresql://awf@db:5432/app"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "db": {"image": "example/database-wrapper:latest", "env_file": "postgres.env"},
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            "DATABASE_URL": database_url,
                            "OLLAMA_HOST": "http://ollama.profile:11434",
                        },
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    profile_env = dict(
        literal_profile_env_from_compose(compose_file, worker_env={"PGPW": "tracked-secret"})
    )

    assert profile_env["DATABASE_URL"] == database_url
    assert profile_env["OLLAMA_HOST"] == "http://ollama.profile:11434"
    assert "tracked-secret" not in "\x00".join(profile_env.values())


@pytest.mark.unit
def test_literal_profile_env_from_compose_redacts_local_postgres_url_userinfo_passwords(
    tmp_path: Path,
) -> None:
    """Local Postgres URL bypass must not carry untracked embedded passwords."""

    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            "DATABASE_URL": ("postgresql://awf:literal-secret@postgres:5432/awf"),
                            "AWF_DATABASE_URL": (
                                "postgresql+asyncpg://awf:awf-secret@postgres:5432/awf"
                            ),
                            "AWF_TEST_DATABASE_URL": (
                                "postgresql+asyncpg://awf:test-secret@postgres:5432/awf"
                            ),
                            "OLLAMA_HOST": "http://ollama.profile:11434",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    profile_env = literal_profile_env_from_compose(compose_file, worker_env={})

    assert ("OLLAMA_HOST", "http://ollama.profile:11434") in profile_env
    carried = dict(profile_env)
    assert "DATABASE_URL" not in carried
    assert "AWF_DATABASE_URL" not in carried
    assert "AWF_TEST_DATABASE_URL" not in carried
    blob = "\x00".join(v for _k, v in profile_env)
    assert "literal-secret" not in blob
    assert "awf-secret" not in blob
    assert "test-secret" not in blob


@pytest.mark.unit
@pytest.mark.parametrize(
    "database_url,secret",
    [
        ("postgres://postgres@postgres:5432/app?password=s3cr3t", "s3cr3t"),
        ("postgres://postgres@postgres:5432/app#token=s3cr3t", "s3cr3t"),
        ("postgresql://postgres@postgres:5432/app?password=s3cr3t", "s3cr3t"),
        ("postgresql+asyncpg://postgres@postgres:5432/app#token=s3cr3t", "s3cr3t"),
    ],
)
def test_literal_profile_env_from_compose_redacts_local_postgres_url_query_credentials(
    tmp_path: Path,
    database_url: str,
    secret: str,
) -> None:
    """Local Postgres URL bypass must not carry query or fragment credentials."""

    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            "DATABASE_URL": database_url,
                            "OLLAMA_HOST": "http://ollama.profile:11434",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    profile_env = literal_profile_env_from_compose(compose_file, worker_env={})

    assert ("OLLAMA_HOST", "http://ollama.profile:11434") in profile_env
    assert "DATABASE_URL" not in dict(profile_env)
    assert secret not in "\x00".join(v for _k, v in profile_env)


@pytest.mark.unit
@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql://awf@postgres:5432/app/"
        + quote("https://callback.example/cb?access_token=nested-secret", safe=""),
        "postgresql://awf@postgres:5432/app#"
        + quote("https://callback.example/cb?access_token=nested-secret", safe=""),
    ],
)
def test_literal_profile_env_from_compose_redacts_encoded_nested_url_credentials(
    tmp_path: Path,
    database_url: str,
) -> None:
    """Local Postgres URL bypass must inspect decoded nested URL credentials."""

    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            "DATABASE_URL": database_url,
                            "OLLAMA_HOST": "http://ollama.profile:11434",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    profile_env = literal_profile_env_from_compose(compose_file, worker_env={})

    assert ("OLLAMA_HOST", "http://ollama.profile:11434") in profile_env
    assert "DATABASE_URL" not in dict(profile_env)
    assert "nested-secret" not in "\x00".join(v for _k, v in profile_env)


@pytest.mark.unit
def test_literal_profile_env_from_compose_redacts_libpq_keyword_password(
    tmp_path: Path,
) -> None:
    """Keyword DSN password fields are database credentials, not safe config."""

    compose_file = tmp_path / "compose.yml"
    keyword_dsn = "host=postgres user=app password=s3cr3t dbname=app"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            "DATABASE_URL": keyword_dsn,
                            "OLLAMA_HOST": "http://ollama.profile:11434",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    profile_env = literal_profile_env_from_compose(compose_file, worker_env={})

    carried = dict(profile_env)
    assert carried["OLLAMA_HOST"] == "http://ollama.profile:11434"
    assert "DATABASE_URL" not in carried
    assert "s3cr3t" not in "\x00".join(v for _k, v in profile_env)


@pytest.mark.unit
def test_literal_profile_env_from_compose_redacts_libpq_keyword_sslpassword(
    tmp_path: Path,
) -> None:
    """Keyword DSN sslpassword fields are TLS key passphrases, not safe config."""

    compose_file = tmp_path / "compose.yml"
    keyword_dsn = "host=postgres user=app sslpassword=s3cr3t dbname=app"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            "DATABASE_URL": keyword_dsn,
                            "OLLAMA_HOST": "http://ollama.profile:11434",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    profile_env = literal_profile_env_from_compose(compose_file, worker_env={})

    carried = dict(profile_env)
    assert carried["OLLAMA_HOST"] == "http://ollama.profile:11434"
    assert "DATABASE_URL" not in carried
    assert "s3cr3t" not in "\x00".join(v for _k, v in profile_env)


@pytest.mark.unit
def test_literal_profile_env_from_compose_preserves_libpq_keyword_dsn_without_secret_fields(
    tmp_path: Path,
) -> None:
    """Keyword DSN values remain profile config when no secret field is present."""

    compose_file = tmp_path / "compose.yml"
    keyword_dsn = "host=postgres user=app dbname=app sslmode=require"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            "DATABASE_URL": keyword_dsn,
                            "OLLAMA_HOST": "http://ollama.profile:11434",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    profile_env = dict(literal_profile_env_from_compose(compose_file, worker_env={}))

    assert profile_env["DATABASE_URL"] == keyword_dsn
    assert profile_env["OLLAMA_HOST"] == "http://ollama.profile:11434"


@pytest.mark.unit
def test_literal_profile_env_from_compose_redacts_generic_url_userinfo(
    tmp_path: Path,
) -> None:
    """Hosted profile env never carries arbitrary URL userinfo credentials."""

    compose_file = tmp_path / "compose.yml"
    secret_url = "https://user:opaque@example.com/api"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            "SERVICE_URL": secret_url,
                            "OLLAMA_HOST": "http://ollama.profile:11434",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    profile_env = literal_profile_env_from_compose(compose_file, worker_env={})

    carried = dict(profile_env)
    assert carried["OLLAMA_HOST"] == "http://ollama.profile:11434"
    assert "SERVICE_URL" not in carried
    assert "opaque" not in "\x00".join(v for _k, v in profile_env)


@pytest.mark.unit
def test_literal_profile_env_from_compose_redacts_prefixed_token_secret_config_fields(
    tmp_path: Path,
) -> None:
    """Neutral config env values can still embed token or secret fields."""

    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump({"services": {"agent": {"image": "agent:latest"}}}),
        encoding="utf-8",
    )

    profile_env = literal_profile_env_from_compose(
        compose_file,
        compose_env={
            "APP_CONFIG": "api_token=token-secret",
            "JSON_CONFIG": '{"bearerToken":"bearer-secret"}',
            "SLACK_CONFIG": "slack_signing_secret=signing-secret",
            "CAMEL_CONFIG": '{"slackSigningSecret":"camel-secret"}',
            "SAFE_CONFIG": "feature=enabled",
        },
        worker_env={},
    )

    carried = dict(profile_env)
    assert carried == {"SAFE_CONFIG": "feature=enabled"}
    blob = "\x00".join(v for _k, v in profile_env)
    assert "token-secret" not in blob
    assert "bearer-secret" not in blob
    assert "signing-secret" not in blob
    assert "camel-secret" not in blob


@pytest.mark.unit
def test_literal_profile_env_from_compose_redacts_prefixed_password_config_fields(
    tmp_path: Path,
) -> None:
    """Neutral config env values can still embed prefixed password fields."""

    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump({"services": {"agent": {"image": "agent:latest"}}}),
        encoding="utf-8",
    )

    profile_env = literal_profile_env_from_compose(
        compose_file,
        compose_env={
            "APP_CONFIG": '{"db_password":"db-secret"}',
            "CAMEL_CONFIG": '{"databasePassword":"camel-secret"}',
            "SAFE_CONFIG": "feature=enabled",
        },
        worker_env={},
    )

    carried = dict(profile_env)
    assert carried == {"SAFE_CONFIG": "feature=enabled"}
    blob = "\x00".join(v for _k, v in profile_env)
    assert "db-secret" not in blob
    assert "camel-secret" not in blob


@pytest.mark.unit
def test_literal_profile_env_from_compose_redacts_encoded_nested_url_query_credentials(
    tmp_path: Path,
) -> None:
    """Decoded nested URL parameter values must not carry bearer material."""

    nested_secret = "bearer-secret"
    nested_url = f"https://app.internal/cb?access_token={nested_secret}"
    callback_url = f"https://proxy.internal/cb?next={quote(nested_url, safe='')}"
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            "CALLBACK_URL": callback_url,
                            "OLLAMA_HOST": "http://ollama.profile:11434",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    profile_env = literal_profile_env_from_compose(compose_file, worker_env={})

    carried = dict(profile_env)
    assert carried["OLLAMA_HOST"] == "http://ollama.profile:11434"
    assert "CALLBACK_URL" not in carried
    assert nested_secret not in "\x00".join(v for _k, v in profile_env)


@pytest.mark.unit
def test_literal_profile_env_from_compose_redacts_encoded_nested_url_path_credentials(
    tmp_path: Path,
) -> None:
    """Decoded nested URL path values must not carry bearer material."""

    nested_secret = "bearer-secret"
    nested_url = f"https://app.internal/cb?access_token={nested_secret}"
    callback_url = f"https://proxy.internal/{quote(nested_url, safe='')}"
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            "CALLBACK_URL": callback_url,
                            "OLLAMA_HOST": "http://ollama.profile:11434",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    profile_env = literal_profile_env_from_compose(compose_file, worker_env={})

    carried = dict(profile_env)
    assert carried["OLLAMA_HOST"] == "http://ollama.profile:11434"
    assert "CALLBACK_URL" not in carried
    assert nested_secret not in "\x00".join(v for _k, v in profile_env)


@pytest.mark.unit
def test_literal_profile_env_from_compose_redacts_postgres_password_from_service_env_file(
    tmp_path: Path,
) -> None:
    """A DB service ``env_file`` can declare the password redaction source."""

    env_file = tmp_path / "postgres.env"
    env_file.write_text("POSTGRES_PASSWORD=env-file-secret\n", encoding="utf-8")

    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "postgres": {
                        "image": "postgres:16-alpine",
                        "env_file": [str(env_file)],
                        "environment": {
                            "POSTGRES_USER": "awf",
                            "POSTGRES_DB": "awf",
                        },
                    },
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            "OLLAMA_HOST": "http://ollama.profile:11434",
                            "DATABASE_URL": "postgresql://awf:env-file-secret@postgres:5432/awf",
                            "AWF_DATABASE_URL": (
                                "postgresql+asyncpg://awf:env-file-secret@postgres:5432/awf"
                            ),
                        },
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    profile_env = literal_profile_env_from_compose(compose_file, worker_env={})

    assert ("OLLAMA_HOST", "http://ollama.profile:11434") in profile_env
    carried = dict(profile_env)
    assert "DATABASE_URL" not in carried
    assert "AWF_DATABASE_URL" not in carried
    assert "env-file-secret" not in "".join(v for _k, v in profile_env)


@pytest.mark.unit
def test_literal_profile_env_from_compose_redacts_postgres_password_from_relative_env_file(
    tmp_path: Path,
) -> None:
    """Relative DB service ``env_file`` paths resolve from the compose file."""

    compose_dir = tmp_path / "compose.d"
    compose_dir.mkdir()
    env_file = compose_dir / "db.env"
    env_file.write_text("POSTGRES_PASSWORD=relative-env-file-secret\n", encoding="utf-8")

    compose_file = compose_dir / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "postgres": {
                        "image": "postgres:16-alpine",
                        "env_file": ["./db.env"],
                        "environment": {
                            "POSTGRES_USER": "awf",
                            "POSTGRES_DB": "awf",
                        },
                    },
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            "OLLAMA_HOST": "http://ollama.profile:11434",
                            "DATABASE_URL": (
                                "postgresql://awf:relative-env-file-secret@postgres:5432/awf"
                            ),
                            "AWF_DATABASE_URL": (
                                "postgresql+asyncpg://awf:relative-env-file-secret"
                                "@postgres:5432/awf"
                            ),
                        },
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    import os

    prev_cwd = Path.cwd()
    os.chdir(tmp_path)
    try:
        profile_env = literal_profile_env_from_compose(compose_file, worker_env={})
    finally:
        os.chdir(prev_cwd)

    assert ("OLLAMA_HOST", "http://ollama.profile:11434") in profile_env
    carried = dict(profile_env)
    assert "DATABASE_URL" not in carried
    assert "AWF_DATABASE_URL" not in carried
    assert "relative-env-file-secret" not in "".join(v for _k, v in profile_env)


@pytest.mark.unit
def test_literal_profile_env_from_compose_redacts_percent_encoded_postgres_password(
    tmp_path: Path,
) -> None:
    """Percent-encoded Postgres URL passwords must still be redacted."""

    raw_password = "p@ss/word"
    encoded_password = quote(raw_password, safe="")

    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "postgres": {
                        "image": "postgres:16-alpine",
                        "environment": {
                            "POSTGRES_USER": "awf",
                            "POSTGRES_PASSWORD": raw_password,
                            "POSTGRES_DB": "awf",
                        },
                    },
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            "OLLAMA_HOST": "http://ollama.profile:11434",
                            "DATABASE_URL": (
                                f"postgresql://awf:{encoded_password}@postgres:5432/awf"
                            ),
                            "AWF_DATABASE_URL": (
                                f"postgresql+asyncpg://awf:{encoded_password}@postgres:5432/awf"
                            ),
                        },
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    profile_env = literal_profile_env_from_compose(compose_file, worker_env={})

    assert ("OLLAMA_HOST", "http://ollama.profile:11434") in profile_env
    carried = dict(profile_env)
    assert "DATABASE_URL" not in carried
    assert "AWF_DATABASE_URL" not in carried
    assert raw_password not in "".join(v for _k, v in profile_env)
    assert encoded_password not in "".join(v for _k, v in profile_env)


@pytest.mark.unit
def test_literal_profile_env_from_compose_redacts_malformed_url_with_postgres_password(
    tmp_path: Path,
) -> None:
    """Malformed URL-like values still use fallback password redaction."""

    password = "env-file-secret"
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            "OLLAMA_HOST": "http://ollama.profile:11434",
                        },
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    profile_env = literal_profile_env_from_compose(
        compose_file,
        compose_env={
            "OLLAMA_HOST": "http://ollama.profile:11434",
            "AWF_DATABASE_URL": f"postgresql://awf:{password}@[::1",
        },
        worker_env={},
        postgres_passwords=frozenset({password}),
    )

    carried = dict(profile_env)
    assert carried.get("OLLAMA_HOST") == "http://ollama.profile:11434"
    assert "AWF_DATABASE_URL" not in carried
    assert password not in "".join(v for _k, v in profile_env)


@pytest.mark.unit
def test_local_postgres_database_url_without_tracked_password_rejects_malformed_url() -> None:
    """Malformed local Postgres DSNs are not safe hosted profile config."""

    assert (
        _local_postgres_database_url_without_tracked_password(
            "DATABASE_URL",
            "postgresql://awf@[::1",
            local_postgres_hostnames=frozenset({"postgres"}),
            postgres_passwords=frozenset(),
        )
        is False
    )


@pytest.mark.unit
def test_literal_profile_env_from_compose_redacts_encoded_postgres_password_in_literal(
    tmp_path: Path,
) -> None:
    """Non-URL literals embedding encoded Postgres passwords are redacted too."""

    raw_password = "p@ss/word"
    encoded_password = quote(raw_password, safe="")
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            "OLLAMA_HOST": "http://ollama.profile:11434",
                        },
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    profile_env = literal_profile_env_from_compose(
        compose_file,
        compose_env={
            "OLLAMA_HOST": "http://ollama.profile:11434",
            "POSTGRES_DIAGNOSTIC": f"rendered-password={encoded_password}",
        },
        worker_env={},
        postgres_passwords=frozenset({raw_password}),
    )

    carried = dict(profile_env)
    assert carried.get("OLLAMA_HOST") == "http://ollama.profile:11434"
    assert "POSTGRES_DIAGNOSTIC" not in carried
    assert raw_password not in "".join(v for _k, v in profile_env)
    assert encoded_password not in "".join(v for _k, v in profile_env)
