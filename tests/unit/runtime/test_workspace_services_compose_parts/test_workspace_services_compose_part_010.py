"""No-Docker compose coverage for Postgres profile-env redaction (part 10)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from awf.profiles.compose import literal_profile_env_from_compose


@pytest.mark.unit
def test_literal_profile_env_from_compose_carries_values_without_postgres_service(
    tmp_path: Path,
) -> None:
    """When no postgres service declares a password, nothing is redacted.

    A profile without a postgres sidecar (e.g. a pure-Ollama profile) has no
    generated DB password to redact; its agent env literals are carried as
    before. This guards against over-redacting when the postgres password is
    absent.
    """

    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            "OLLAMA_HOST": "http://ollama.profile:11434",
                            # A literal that merely contains a colon-separated
                            # token must NOT be mistaken for a secret when no
                            # postgres password is declared to redact.
                            "APP_BASE_URL": "http://app:8080",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    profile_env = literal_profile_env_from_compose(compose_file, worker_env={})
    assert ("OLLAMA_HOST", "http://ollama.profile:11434") in profile_env
    assert ("APP_BASE_URL", "http://app:8080") in profile_env


@pytest.mark.unit
def test_literal_profile_env_from_compose_redacts_postgres_password_under_nonstandard_service_name(
    tmp_path: Path,
) -> None:
    """A custom profile may name its database service ``db`` / ``database``
    (or anything else) while still setting ``POSTGRES_PASSWORD`` and expanding
    that same password into the agent env ``DATABASE_URL`` /
    ``AWF_DATABASE_URL``. The redaction source must collect
    ``POSTGRES_PASSWORD`` from every compose service, not only from a service
    literally named ``postgres``; otherwise ``file_postgres_password`` stays
    ``None`` for a valid custom profile and ``literal_profile_env_from_compose``
    carries the rendered DB URL in ``AgentRuntimeExecRequest.profile_env``,
    leaking the workspace credential to the hosted executor despite the
    secret-free contract.

    Regression for PR #751 thread PRRT_kwDOSJAM6s6PWUIl.
    """

    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    # A custom-profile DB sidecar named ``db`` (not ``postgres``)
                    # still declares ``POSTGRES_PASSWORD`` and shares it with
                    # the agent env DB URLs below.
                    "db": {
                        "image": "postgres:16-alpine",
                        "environment": {
                            "POSTGRES_USER": "awf",
                            "POSTGRES_PASSWORD": "workspace-secret",
                            "POSTGRES_DB": "awf",
                        },
                    },
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            # Non-secret profile literal -> carried (must survive).
                            "OLLAMA_HOST": "http://ollama.profile:11434",
                            # AWF-expanded DB URLs embed the shared postgres
                            # password -> must be skipped (secret-bearing).
                            "DATABASE_URL": ("postgresql://awf:workspace-secret@db:5432/awf"),
                            "AWF_DATABASE_URL": (
                                "postgresql+asyncpg://awf:workspace-secret@db:5432/awf"
                            ),
                            "AWF_TEST_DATABASE_URL": (
                                "postgresql+asyncpg://awf:workspace-secret@db:5432/awf"
                            ),
                        },
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    profile_env = literal_profile_env_from_compose(compose_file, worker_env={})

    # Non-secret profile literal is still carried to the hosted executor.
    assert ("OLLAMA_HOST", "http://ollama.profile:11434") in profile_env
    # Secret-bearing expanded values are NOT carried to the hosted executor.
    carried = dict(profile_env)
    assert "DATABASE_URL" not in carried
    assert "AWF_DATABASE_URL" not in carried
    assert "AWF_TEST_DATABASE_URL" not in carried
    # The workspace DB password never reaches the hosted request object.
    assert "workspace-secret" not in "".join(v for _k, v in profile_env)


@pytest.mark.unit
def test_literal_profile_env_from_compose_redacts_all_distinct_postgres_passwords(
    tmp_path: Path,
) -> None:
    """A profile may run several DB sidecars each declaring a *different*
    ``POSTGRES_PASSWORD``. The redaction source must collect every declared
    value, not only the first; otherwise a rendered agent env value (e.g. a
    second service's ``WAREHOUSE_URL``) embedding a later service's password
    slips past a redaction that only compares against the first service's
    password, leaking the workspace credential to the hosted executor.

    Regression for PR #751 thread PRRT_kwDOSJAM6s6PWsKk.
    """

    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "postgres": {
                        "image": "postgres:16-alpine",
                        "environment": {
                            "POSTGRES_USER": "awf",
                            "POSTGRES_PASSWORD": "workspace-secret",
                            "POSTGRES_DB": "awf",
                        },
                    },
                    "warehouse": {
                        "image": "postgres:16-alpine",
                        "environment": {
                            "POSTGRES_USER": "awf",
                            "POSTGRES_PASSWORD": "warehouse-secret",
                            "POSTGRES_DB": "warehouse",
                        },
                    },
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            # Non-secret profile literal -> carried (must survive).
                            "OLLAMA_HOST": "http://ollama.profile:11434",
                            # First DB URL embeds the first service's password.
                            "DATABASE_URL": ("postgresql://awf:workspace-secret@postgres:5432/awf"),
                            # Second DB URL embeds the *other* service's password
                            # -> must also be skipped (not only the first one).
                            "WAREHOUSE_URL": (
                                "postgresql://awf:warehouse-secret@warehouse:5432/warehouse"
                            ),
                        },
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    profile_env = literal_profile_env_from_compose(compose_file, worker_env={})

    # Non-secret profile literal is still carried to the hosted executor.
    assert ("OLLAMA_HOST", "http://ollama.profile:11434") in profile_env
    # Secret-bearing expanded values are NOT carried, regardless of which
    # service's password they embed.
    carried = dict(profile_env)
    assert "DATABASE_URL" not in carried
    assert "WAREHOUSE_URL" not in carried
    # Neither workspace DB password reaches the hosted request object.
    assert "workspace-secret" not in "".join(v for _k, v in profile_env)
    assert "warehouse-secret" not in "".join(v for _k, v in profile_env)


@pytest.mark.unit
def test_literal_profile_env_from_compose_redacts_postgres_password_interpolation_default(
    tmp_path: Path,
) -> None:
    """A service may express ``POSTGRES_PASSWORD`` via Compose
    interpolation/defaults (e.g. ``${POSTGRES_PASSWORD:-fallback}``), which
    ComposeManager does not expand (it only expands bare
    ``${AWF_POSTGRES_PASSWORD}``). The rendered service env therefore retains the
    ``${...}`` form, and Docker Compose resolves it against the worker env at
    stack launch. The redaction source must resolve each declared password
    against the worker env (mirroring Compose) so a rendered agent env DB URL
    that embeds the *resolved* worker value is redacted; comparing only against
    the raw ``${...}`` placeholder string would miss the expanded secret.

    Regression for PR #751 thread PRRT_kwDOSJAM6s6PWsKk.
    """

    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "postgres": {
                        "image": "postgres:16-alpine",
                        "environment": {
                            "POSTGRES_USER": "awf",
                            # Interpolation with a default: Compose resolves this
                            # against the worker env at stack launch.
                            "POSTGRES_PASSWORD": "${POSTGRES_PASSWORD:-fallback-pw}",
                            "POSTGRES_DB": "awf",
                        },
                    },
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            # Non-secret profile literal -> carried (must survive).
                            "OLLAMA_HOST": "http://ollama.profile:11434",
                            # A rendered DB URL embedding the *resolved* worker
                            # value -> must be skipped (secret-bearing).
                            "DATABASE_URL": ("postgresql://awf:resolved-pw@postgres:5432/awf"),
                        },
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    # Worker env supplies the resolved password that Compose would inject.
    profile_env = literal_profile_env_from_compose(
        compose_file, worker_env={"POSTGRES_PASSWORD": "resolved-pw"}
    )

    # Non-secret profile literal is still carried to the hosted executor.
    assert ("OLLAMA_HOST", "http://ollama.profile:11434") in profile_env
    # The resolved-password-bearing DB URL is NOT carried.
    carried = dict(profile_env)
    assert "DATABASE_URL" not in carried
    # The resolved worker password never reaches the hosted request object.
    assert "resolved-pw" not in "".join(v for _k, v in profile_env)
