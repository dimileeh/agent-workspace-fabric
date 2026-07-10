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
def test_literal_profile_env_from_compose_preserves_hosted_bitbucket_rewrites(
    tmp_path: Path,
) -> None:
    """Hosted jobs keep Bitbucket agent rewrites when no local askpass mount exists."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            "GIT_CONFIG_KEY_0": (
                                "url.https://x-bitbucket-api-token-auth@bitbucket.org/.insteadOf"
                            ),
                            "GIT_CONFIG_VALUE_0": "https://bitbucket.org/",
                            "GIT_CONFIG_KEY_1": (
                                "url.https://x-bitbucket-api-token-auth@bitbucket.org/.insteadOf"
                            ),
                            "GIT_CONFIG_VALUE_1": "https://bitbucket.org:443/",
                            "GIT_CONFIG_KEY_2": (
                                "url.https://x-bitbucket-api-token-auth@bitbucket.org/.insteadOf"
                            ),
                            "GIT_CONFIG_VALUE_2": "git@bitbucket.org:",
                            "GIT_CONFIG_KEY_3": (
                                "url.https://x-bitbucket-api-token-auth@bitbucket.org/.insteadOf"
                            ),
                            "GIT_CONFIG_VALUE_3": "ssh://git@bitbucket.org/",
                            "GIT_CONFIG_KEY_4": (
                                "url.https://x-bitbucket-api-token-auth@bitbucket.org/.insteadOf"
                            ),
                            "GIT_CONFIG_VALUE_4": "ssh://git@bitbucket.org:22/",
                            "GIT_CONFIG_COUNT": "5",
                            "APP_BASE_URL": "http://app:8080",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    carried = dict(literal_profile_env_from_compose(compose_file, worker_env={}))

    assert carried["APP_BASE_URL"] == "http://app:8080"
    assert carried["GIT_CONFIG_COUNT"] == "5"
    assert (
        carried["GIT_CONFIG_KEY_0"]
        == "url.https://x-bitbucket-api-token-auth@bitbucket.org/.insteadOf"
    )
    assert carried["GIT_CONFIG_VALUE_0"] == "https://bitbucket.org/"
    assert (
        carried["GIT_CONFIG_KEY_1"]
        == "url.https://x-bitbucket-api-token-auth@bitbucket.org/.insteadOf"
    )
    assert carried["GIT_CONFIG_VALUE_1"] == "https://bitbucket.org:443/"
    assert (
        carried["GIT_CONFIG_KEY_2"]
        == "url.https://x-bitbucket-api-token-auth@bitbucket.org/.insteadOf"
    )
    assert carried["GIT_CONFIG_VALUE_2"] == "git@bitbucket.org:"
    assert (
        carried["GIT_CONFIG_KEY_3"]
        == "url.https://x-bitbucket-api-token-auth@bitbucket.org/.insteadOf"
    )
    assert carried["GIT_CONFIG_VALUE_3"] == "ssh://git@bitbucket.org/"
    assert (
        carried["GIT_CONFIG_KEY_4"]
        == "url.https://x-bitbucket-api-token-auth@bitbucket.org/.insteadOf"
    )
    assert carried["GIT_CONFIG_VALUE_4"] == "ssh://git@bitbucket.org:22/"


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
def test_literal_profile_env_from_compose_redacts_carried_git_config_url_userinfo(
    tmp_path: Path,
) -> None:
    """Reindexed profile git config must keep embedded URL credentials out."""
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
                            "GIT_CONFIG_KEY_1": ("url.https://github-token@github.com/.insteadOf"),
                            "GIT_CONFIG_VALUE_1": "https://github.com/",
                            "GIT_CONFIG_KEY_2": "url.https://github.com/.insteadOf",
                            "GIT_CONFIG_VALUE_2": "https://github-token@github.com/",
                            "GIT_CONFIG_COUNT": "3",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    profile_env = literal_profile_env_from_compose(compose_file, worker_env={})
    carried = dict(profile_env)

    assert carried["GIT_CONFIG_COUNT"] == "1"
    assert carried["GIT_CONFIG_KEY_0"] == "user.email"
    assert carried["GIT_CONFIG_VALUE_0"] == "agent@example.com"
    assert "GIT_CONFIG_KEY_1" not in carried
    assert "GIT_CONFIG_VALUE_1" not in carried
    blob = "\x00".join(f"{key}={value}" for key, value in profile_env)
    assert "github-token" not in blob


@pytest.mark.unit
def test_literal_profile_env_from_compose_redacts_generic_git_config_pair_url_userinfo(
    tmp_path: Path,
) -> None:
    """Dropping one Git config member must not leave a malformed protocol block."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            "GIT_CONFIG_KEY_0": "user.email",
                            "GIT_CONFIG_VALUE_0": "agent@example.com",
                            "GIT_CONFIG_KEY_1": ("url.https://github-token@github.com/.insteadOf"),
                            "GIT_CONFIG_VALUE_1": "https://github.com/",
                            "GIT_CONFIG_KEY_2": "url.https://github.com/.insteadOf",
                            "GIT_CONFIG_VALUE_2": "https://github-token@github.com/",
                            "GIT_CONFIG_COUNT": "3",
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

    assert carried["APP_BASE_URL"] == "http://app:8080"
    assert carried["GIT_CONFIG_COUNT"] == "1"
    assert carried["GIT_CONFIG_KEY_0"] == "user.email"
    assert carried["GIT_CONFIG_VALUE_0"] == "agent@example.com"
    assert "GIT_CONFIG_KEY_1" not in carried
    assert "GIT_CONFIG_VALUE_1" not in carried
    assert "GIT_CONFIG_KEY_2" not in carried
    assert "GIT_CONFIG_VALUE_2" not in carried
    blob = "\x00".join(f"{key}={value}" for key, value in profile_env)
    assert "github-token" not in blob


@pytest.mark.unit
def test_literal_profile_env_from_compose_preserves_safe_ssh_git_config_rewrite_key(
    tmp_path: Path,
) -> None:
    """The standard SSH ``git`` username in a rewrite key is not credential material."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            "GIT_CONFIG_KEY_0": "user.email",
                            "GIT_CONFIG_VALUE_0": "agent@example.com",
                            "GIT_CONFIG_KEY_1": ("url.ssh://git@github.com/.insteadOf"),
                            "GIT_CONFIG_VALUE_1": "https://github.com/",
                            "GIT_CONFIG_KEY_2": ("url.https://github-token@github.com/.insteadOf"),
                            "GIT_CONFIG_VALUE_2": "https://github.com/",
                            "GIT_CONFIG_KEY_3": "url.ssh://git@github.com/.insteadOf",
                            "GIT_CONFIG_VALUE_3": "https://github-token@github.com/",
                            "GIT_CONFIG_COUNT": "4",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    profile_env = literal_profile_env_from_compose(compose_file, worker_env={})
    carried = dict(profile_env)

    assert carried["GIT_CONFIG_COUNT"] == "2"
    assert carried["GIT_CONFIG_KEY_0"] == "user.email"
    assert carried["GIT_CONFIG_VALUE_0"] == "agent@example.com"
    assert carried["GIT_CONFIG_KEY_1"] == "url.ssh://git@github.com/.insteadOf"
    assert carried["GIT_CONFIG_VALUE_1"] == "https://github.com/"
    assert "GIT_CONFIG_KEY_2" not in carried
    assert "GIT_CONFIG_VALUE_2" not in carried
    assert "GIT_CONFIG_KEY_3" not in carried
    assert "GIT_CONFIG_VALUE_3" not in carried
    blob = "\x00".join(f"{key}={value}" for key, value in profile_env)
    assert "github-token" not in blob


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


@pytest.mark.unit
def test_literal_profile_env_from_compose_preserves_safe_ssh_repository_url_literal(
    tmp_path: Path,
) -> None:
    """A passwordless ``ssh://git@...`` repository URL does not carry a secret."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            "REPOSITORY_URL": "ssh://git@github.com/org/repo.git",
                            "APP_BASE_URL": "http://app:8080",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    carried = dict(literal_profile_env_from_compose(compose_file, worker_env={}))

    assert carried["REPOSITORY_URL"] == "ssh://git@github.com/org/repo.git"
    assert carried["APP_BASE_URL"] == "http://app:8080"


@pytest.mark.unit
def test_literal_profile_env_from_compose_skips_url_userinfo_literals(
    tmp_path: Path,
) -> None:
    """Hosted profile env must not carry credentials embedded in URL userinfo."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            "REDIS_URL": "redis://:s3cr3t@redis:6379/0",
                            "CACHE_URL": "redis://redis:6379/0",
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

    assert "REDIS_URL" not in carried
    assert carried["CACHE_URL"] == "redis://redis:6379/0"
    assert carried["APP_BASE_URL"] == "http://app:8080"
    blob = "\x00".join(f"{key}={value}" for key, value in profile_env)
    assert "s3cr3t" not in blob


@pytest.mark.unit
def test_literal_profile_env_from_compose_skips_url_query_password_literals(
    tmp_path: Path,
) -> None:
    """Hosted profile env must not carry credentials embedded in URL queries."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            "DATABASE_URL": "postgresql://db/app?password=s3cr3t",
                            "JDBC_DATABASE_URL": (
                                "jdbc:mysql://db/app?user=app&password=jdbc-secret"
                            ),
                            "CACHE_URL": "redis://redis:6379/0",
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

    assert "DATABASE_URL" not in carried
    assert "JDBC_DATABASE_URL" not in carried
    assert carried["CACHE_URL"] == "redis://redis:6379/0"
    assert carried["APP_BASE_URL"] == "http://app:8080"
    blob = "\x00".join(f"{key}={value}" for key, value in profile_env)
    assert "s3cr3t" not in blob
    assert "jdbc-secret" not in blob


@pytest.mark.unit
def test_literal_profile_env_from_compose_skips_url_query_token_literals(
    tmp_path: Path,
) -> None:
    """Hosted profile env must not carry token-bearing URL query parameters."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            "CALLBACK_URL": "https://proxy.local/cb?token=query-token",
                            "SAFE_CALLBACK_URL": "https://proxy.local/cb?next=/ready",
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

    assert "CALLBACK_URL" not in carried
    assert carried["SAFE_CALLBACK_URL"] == "https://proxy.local/cb?next=/ready"
    assert carried["APP_BASE_URL"] == "http://app:8080"
    blob = "\x00".join(f"{key}={value}" for key, value in profile_env)
    assert "query-token" not in blob


@pytest.mark.unit
def test_literal_profile_env_from_compose_skips_url_query_api_key_literals(
    tmp_path: Path,
) -> None:
    """Hosted profile env must not carry API-key URL query parameters."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            "CALLBACK_URL": "https://api.example/cb?api_key=sk-live",
                            "MAPS_URL": (
                                "https://maps.googleapis.com/maps/api/geocode/json?key=AIza-secret"
                            ),
                            "OAUTH_CALLBACK_URL": (
                                "https://api.example/cb?client_secret=oauth-secret"
                            ),
                            "SAFE_MAPS_URL": (
                                "https://maps.googleapis.com/maps/api/geocode/json?address=Boston"
                            ),
                            "SAFE_CALLBACK_URL": "https://api.example/cb?next=/ready",
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

    assert "CALLBACK_URL" not in carried
    assert "MAPS_URL" not in carried
    assert "OAUTH_CALLBACK_URL" not in carried
    assert (
        carried["SAFE_MAPS_URL"]
        == "https://maps.googleapis.com/maps/api/geocode/json?address=Boston"
    )
    assert carried["SAFE_CALLBACK_URL"] == "https://api.example/cb?next=/ready"
    assert carried["APP_BASE_URL"] == "http://app:8080"
    blob = "\x00".join(f"{key}={value}" for key, value in profile_env)
    assert "sk-live" not in blob
    assert "AIza-secret" not in blob
    assert "oauth-secret" not in blob


@pytest.mark.unit
def test_literal_profile_env_from_compose_skips_subscription_key_header_literals(
    tmp_path: Path,
) -> None:
    """Neutral env names must not carry Azure/APIM subscription-key headers."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            "REQUEST_HEADERS": (
                                '{"Ocp-Apim-Subscription-Key":"profile-subscription-key"}'
                            ),
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

    assert "REQUEST_HEADERS" not in carried
    assert carried["APP_BASE_URL"] == "http://app:8080"
    blob = "\x00".join(f"{key}={value}" for key, value in profile_env)
    assert "profile-subscription-key" not in blob


@pytest.mark.unit
def test_literal_profile_env_from_compose_skips_url_query_subscription_key_literals(
    tmp_path: Path,
) -> None:
    """Hosted profile env must not carry subscription-key URL query parameters."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            "CALLBACK_URL": (
                                "https://api.example/cb?subscription-key=query-subscription-key"
                            ),
                            "SAFE_CALLBACK_URL": "https://api.example/cb?next=/ready",
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

    assert "CALLBACK_URL" not in carried
    assert carried["SAFE_CALLBACK_URL"] == "https://api.example/cb?next=/ready"
    assert carried["APP_BASE_URL"] == "http://app:8080"
    blob = "\x00".join(f"{key}={value}" for key, value in profile_env)
    assert "query-subscription-key" not in blob


@pytest.mark.unit
def test_literal_profile_env_from_compose_skips_url_fragment_credential_literals(
    tmp_path: Path,
) -> None:
    """Hosted profile env must not carry credentials embedded in URL fragments."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            "CALLBACK_URL": ("https://app.example/cb#access_token=fragment-token"),
                            "SAFE_CALLBACK_URL": "https://app.example/cb#section=ready",
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

    assert "CALLBACK_URL" not in carried
    assert carried["SAFE_CALLBACK_URL"] == "https://app.example/cb#section=ready"
    assert carried["APP_BASE_URL"] == "http://app:8080"
    blob = "\x00".join(f"{key}={value}" for key, value in profile_env)
    assert "fragment-token" not in blob


@pytest.mark.unit
def test_literal_profile_env_from_compose_skips_url_path_credential_literals(
    tmp_path: Path,
) -> None:
    """Hosted profile env must not carry credentials embedded in URL path params."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            "CALLBACK_URL": "https://app.example/cb;password=path-secret",
                            "SAFE_CALLBACK_URL": "https://app.example/cb;mode=ready",
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

    assert "CALLBACK_URL" not in carried
    assert carried["SAFE_CALLBACK_URL"] == "https://app.example/cb;mode=ready"
    assert carried["APP_BASE_URL"] == "http://app:8080"
    blob = "\x00".join(f"{key}={value}" for key, value in profile_env)
    assert "path-secret" not in blob


@pytest.mark.unit
def test_literal_profile_env_from_compose_skips_embedded_url_userinfo_literals(
    tmp_path: Path,
) -> None:
    """Hosted profile env must not carry credentials embedded in argument text."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            "EXTRA_ARGS": "--broker=redis://:s3cr3t@redis:6379/0 --verbose",
                            "SAFE_ARGS": "--broker=redis://redis:6379/0 --verbose",
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

    assert "EXTRA_ARGS" not in carried
    assert carried["SAFE_ARGS"] == "--broker=redis://redis:6379/0 --verbose"
    assert carried["APP_BASE_URL"] == "http://app:8080"
    blob = "\x00".join(f"{key}={value}" for key, value in profile_env)
    assert "s3cr3t" not in blob


@pytest.mark.unit
def test_literal_profile_env_from_compose_skips_nested_url_userinfo_literals(
    tmp_path: Path,
) -> None:
    """Safe-looking outer URLs must not carry credentialed nested URLs."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            "CALLBACK_URL": (
                                "https://proxy.local/cb?target=redis://:s3cr3t@redis:6379/0"
                            ),
                            "SAFE_CALLBACK_URL": (
                                "https://proxy.local/cb?target=redis://redis:6379/0"
                            ),
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

    assert "CALLBACK_URL" not in carried
    assert carried["SAFE_CALLBACK_URL"] == "https://proxy.local/cb?target=redis://redis:6379/0"
    assert carried["APP_BASE_URL"] == "http://app:8080"
    blob = "\x00".join(f"{key}={value}" for key, value in profile_env)
    assert "s3cr3t" not in blob


@pytest.mark.unit
def test_literal_profile_env_from_compose_skips_external_postgres_url_userinfo(
    tmp_path: Path,
) -> None:
    """External Postgres DSNs with userinfo are not safe hosted literals."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "postgres": {
                        "image": "postgres:16-alpine",
                        "environment": {
                            "POSTGRES_USER": "awf",
                            "POSTGRES_PASSWORD": "local-compose-password",
                            "POSTGRES_DB": "awf",
                        },
                    },
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            "DATABASE_URL": ("postgresql://user:external-secret@db.example/app"),
                            "REPORTING_DATABASE_URL": (
                                "postgresql+psycopg://report:report-secret@db.example/report"
                            ),
                            "APP_BASE_URL": "https://app.example",
                        },
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    profile_env = literal_profile_env_from_compose(compose_file, worker_env={})
    carried = dict(profile_env)

    assert "DATABASE_URL" not in carried
    assert "REPORTING_DATABASE_URL" not in carried
    assert carried["APP_BASE_URL"] == "https://app.example"
    blob = "\x00".join(f"{key}={value}" for key, value in profile_env)
    assert "external-secret" not in blob
    assert "report-secret" not in blob


@pytest.mark.unit
def test_literal_profile_env_from_compose_skips_git_config_auth_header_value(
    tmp_path: Path,
) -> None:
    """Git env-protocol auth headers must not be carried as hosted literals."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            "GIT_CONFIG_COUNT": "2",
                            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraHeader",
                            "GIT_CONFIG_VALUE_0": "Authorization: Bearer test-token",
                            "GIT_CONFIG_KEY_1": "user.email",
                            "GIT_CONFIG_VALUE_1": "agent@example.com",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    profile_env = literal_profile_env_from_compose(compose_file, worker_env={})
    carried = dict(profile_env)

    assert carried["GIT_CONFIG_COUNT"] == "1"
    assert carried["GIT_CONFIG_KEY_0"] == "user.email"
    assert carried["GIT_CONFIG_VALUE_0"] == "agent@example.com"
    assert "GIT_CONFIG_KEY_1" not in carried
    assert "GIT_CONFIG_VALUE_1" not in carried
    blob = "\x00".join(f"{key}={value}" for key, value in profile_env)
    assert "Authorization" not in blob
    assert "test-token" not in blob


@pytest.mark.unit
def test_literal_profile_env_from_compose_skips_proxy_auth_header_value(
    tmp_path: Path,
) -> None:
    """Neutral env names must not carry proxy authorization header literals."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            "CURL_ARGS": "-H 'Proxy-Authorization: Basic test-proxy-token'",
                            "APP_BASE_URL": "https://app.example",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    profile_env = literal_profile_env_from_compose(compose_file, worker_env={})
    carried = dict(profile_env)

    assert carried["APP_BASE_URL"] == "https://app.example"
    assert "CURL_ARGS" not in carried
    blob = "\x00".join(f"{key}={value}" for key, value in profile_env)
    assert "Proxy-Authorization" not in blob
    assert "test-proxy-token" not in blob


@pytest.mark.unit
def test_literal_profile_env_from_compose_skips_aws_secret_access_key_config_blobs(
    tmp_path: Path,
) -> None:
    """Neutral env names must not carry AWS-prefixed secret access key fields."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            "APP_CONFIG": "AWS_SECRET_ACCESS_KEY=profile-secret",
                            "JSON_CONFIG": '{"aws_secret_access_key":"json-profile-secret"}',
                            "APP_BASE_URL": "https://app.example",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    profile_env = literal_profile_env_from_compose(compose_file, worker_env={})
    carried = dict(profile_env)

    assert carried["APP_BASE_URL"] == "https://app.example"
    assert "APP_CONFIG" not in carried
    assert "JSON_CONFIG" not in carried
    blob = "\x00".join(f"{key}={value}" for key, value in profile_env)
    assert "profile-secret" not in blob
    assert "json-profile-secret" not in blob
