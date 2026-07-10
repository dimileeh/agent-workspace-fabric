"""No-Docker compose coverage for profile-declared workspace services (part 8)."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

import pytest
import yaml

from awf.profiles.compose import literal_profile_env_from_compose


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
