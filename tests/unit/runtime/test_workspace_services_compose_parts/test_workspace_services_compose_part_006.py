"""Hosted profile-env regressions for Bitbucket askpass carry filtering."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from awf.profiles.compose import literal_profile_env_from_compose


@pytest.mark.unit
def test_literal_profile_env_from_compose_skips_mount_backed_bitbucket_askpass(
    tmp_path: Path,
) -> None:
    """AWF's Compose-only Bitbucket askpass mount is not carried to hosted jobs."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            "GIT_ASKPASS": "/run/awf/secrets/bb-askpass.sh",
                            "GIT_TERMINAL_PROMPT": "0",
                            "GIT_CONFIG_KEY_0": (
                                "url.https://x-bitbucket-api-token-auth@bitbucket.org/.insteadOf"
                            ),
                            "GIT_CONFIG_VALUE_0": "https://bitbucket.org/",
                            "GIT_CONFIG_COUNT": "1",
                            "APP_BASE_URL": "http://app:8080",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    profile_env = literal_profile_env_from_compose(compose_file, worker_env={})
    carried = dict(profile_env)

    assert carried.get("APP_BASE_URL") == "http://app:8080"
    assert "GIT_ASKPASS" not in carried
    assert carried.get("GIT_TERMINAL_PROMPT") == "0"
    assert "GIT_CONFIG_COUNT" not in carried
    assert "GIT_CONFIG_KEY_0" not in carried
    assert "GIT_CONFIG_VALUE_0" not in carried
    blob = "\x00".join(f"{key}={value}" for key, value in profile_env)
    assert "/run/awf/secrets/bb-askpass.sh" not in blob
    assert "x-bitbucket-api-token-auth" not in blob


@pytest.mark.unit
def test_literal_profile_env_from_compose_reindexes_remaining_git_config(
    tmp_path: Path,
) -> None:
    """Removing Bitbucket rewrites keeps unrelated profile git config valid."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            "GIT_ASKPASS": "/run/awf/secrets/bb-askpass.sh",
                            "GIT_CONFIG_KEY_0": "user.email",
                            "GIT_CONFIG_VALUE_0": "agent@example.com",
                            "GIT_CONFIG_KEY_1": (
                                "url.https://x-bitbucket-api-token-auth@bitbucket.org/.insteadOf"
                            ),
                            "GIT_CONFIG_VALUE_1": "https://bitbucket.org/",
                            "GIT_CONFIG_COUNT": "2",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    carried = dict(literal_profile_env_from_compose(compose_file, worker_env={}))

    assert carried["GIT_CONFIG_COUNT"] == "1"
    assert carried["GIT_CONFIG_KEY_0"] == "user.email"
    assert carried["GIT_CONFIG_VALUE_0"] == "agent@example.com"
    assert "GIT_CONFIG_KEY_1" not in carried
    assert "GIT_CONFIG_VALUE_1" not in carried


@pytest.mark.unit
def test_literal_profile_env_from_compose_preserves_profile_git_askpass_literal(
    tmp_path: Path,
) -> None:
    """A profile-owned non-AWF askpass literal still reaches hosted profile env."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            "GIT_ASKPASS": "/opt/profile/askpass.sh",
                            "APP_BASE_URL": "http://app:8080",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    profile_env = literal_profile_env_from_compose(compose_file, worker_env={})
    carried = dict(profile_env)

    assert carried.get("GIT_ASKPASS") == "/opt/profile/askpass.sh"
    assert carried.get("APP_BASE_URL") == "http://app:8080"


@pytest.mark.unit
def test_literal_profile_env_from_compose_keeps_short_password_collision_config(
    tmp_path: Path,
) -> None:
    """A short local DB password must not redact unrelated host literals."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "postgres": {
                        "image": "postgres:16-alpine",
                        "environment": {
                            "POSTGRES_USER": "awf",
                            "POSTGRES_PASSWORD": "postgres",
                            "POSTGRES_DB": "awf",
                        },
                    },
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            "POSTGRES_HOST": "postgres",
                            "DATABASE_HOST": "postgres",
                            "DATABASE_URL": "postgresql://awf:postgres@postgres:5432/awf",
                        },
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    carried = dict(literal_profile_env_from_compose(compose_file, worker_env={}))

    assert carried["POSTGRES_HOST"] == "postgres"
    assert carried["DATABASE_HOST"] == "postgres"
    assert "DATABASE_URL" not in carried
