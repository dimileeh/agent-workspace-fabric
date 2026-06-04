"""Unit tests for local service legacy env-file migration."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

_FIXED_NOW = datetime(2026, 6, 4, 12, 0, tzinfo=UTC)


@pytest.mark.unit
def test_legacy_compose_env_migration_creates_root_env_from_example_and_legacy(
    tmp_path: Path,
) -> None:
    """A fresh root env is seeded from the template with legacy values overlaid."""
    from awf.service.env_migration import migrate_legacy_compose_env_file

    root_env = tmp_path / ".env"
    env_example = tmp_path / ".env.example"
    legacy_env = tmp_path / "docker" / "compose" / ".env"
    legacy_env.parent.mkdir(parents=True)
    env_example.write_text(
        "AWF_API_TOKEN=\nAWF_POSTGRES_PASSWORD=template\nCURSOR_API_KEY=\n",
        encoding="utf-8",
    )
    legacy_env.write_text(
        "AWF_API_TOKEN=legacy-token\nCURSOR_API_KEY=legacy-cursor\n",
        encoding="utf-8",
    )

    result = migrate_legacy_compose_env_file(
        canonical_env_file=root_env,
        env_example_file=env_example,
        legacy_env_file=legacy_env,
        now=lambda: _FIXED_NOW,
    )

    assert result.status == "migrated"
    assert result.created_env_file is True
    assert result.imported_keys == ("AWF_API_TOKEN", "CURSOR_API_KEY")
    assert result.conflict_keys == ()
    assert result.backup_path is not None
    assert result.backup_path.name == ".env.legacy-20260604T120000Z.bak"
    assert not legacy_env.exists()
    assert result.backup_path.read_text(encoding="utf-8") == (
        "AWF_API_TOKEN=legacy-token\nCURSOR_API_KEY=legacy-cursor\n"
    )
    assert root_env.read_text(encoding="utf-8") == (
        "AWF_API_TOKEN=legacy-token\nAWF_POSTGRES_PASSWORD=template\nCURSOR_API_KEY=legacy-cursor\n"
    )

    rendered = repr(result.to_dict())
    assert "legacy-token" not in rendered
    assert "legacy-cursor" not in rendered


@pytest.mark.unit
def test_legacy_compose_env_migration_imports_missing_keys_and_keeps_root_conflicts(
    tmp_path: Path,
) -> None:
    """Existing root values stay canonical; only absent legacy keys are appended."""
    from awf.service.env_migration import migrate_legacy_compose_env_file

    root_env = tmp_path / ".env"
    env_example = tmp_path / ".env.example"
    legacy_env = tmp_path / "docker" / "compose" / ".env"
    legacy_env.parent.mkdir(parents=True)
    root_env.write_text(
        "AWF_API_TOKEN=root-token\nOPENAI_API_KEY=root-openai\n",
        encoding="utf-8",
    )
    env_example.write_text("AWF_API_TOKEN=\nOPENAI_API_KEY=\nCURSOR_API_KEY=\n", encoding="utf-8")
    legacy_env.write_text(
        "AWF_API_TOKEN=legacy-token\nCURSOR_API_KEY=legacy-cursor\n",
        encoding="utf-8",
    )

    result = migrate_legacy_compose_env_file(
        canonical_env_file=root_env,
        env_example_file=env_example,
        legacy_env_file=legacy_env,
        now=lambda: _FIXED_NOW,
    )

    assert result.status == "migrated"
    assert result.created_env_file is False
    assert result.imported_keys == ("CURSOR_API_KEY",)
    assert result.conflict_keys == ("AWF_API_TOKEN",)
    assert not legacy_env.exists()
    assert root_env.read_text(encoding="utf-8") == (
        "AWF_API_TOKEN=root-token\n"
        "OPENAI_API_KEY=root-openai\n"
        "\n# Imported from legacy docker/compose/.env by AWF.\n"
        "CURSOR_API_KEY=legacy-cursor\n"
    )

    payload = result.to_dict()
    assert payload["conflict_keys"] == ["AWF_API_TOKEN"]
    assert "legacy-token" not in repr(payload)
    assert "legacy-cursor" not in repr(payload)


@pytest.mark.unit
def test_legacy_compose_env_migration_noops_without_legacy_file(tmp_path: Path) -> None:
    """No legacy compose env means no mutation and no backup."""
    from awf.service.env_migration import migrate_legacy_compose_env_file

    root_env = tmp_path / ".env"

    result = migrate_legacy_compose_env_file(
        canonical_env_file=root_env,
        env_example_file=tmp_path / ".env.example",
        legacy_env_file=tmp_path / "docker" / "compose" / ".env",
        now=lambda: _FIXED_NOW,
    )

    assert result.status == "not_needed"
    assert result.imported_keys == ()
    assert result.backup_path is None
    assert not root_env.exists()


@pytest.mark.unit
def test_legacy_compose_env_migration_noops_when_legacy_path_is_directory(
    tmp_path: Path,
) -> None:
    """A directory named like the legacy env file is not a readable env file."""
    from awf.service.env_migration import migrate_legacy_compose_env_file

    root_env = tmp_path / ".env"
    legacy_env = tmp_path / "docker" / "compose" / ".env"
    legacy_env.mkdir(parents=True)

    result = migrate_legacy_compose_env_file(
        canonical_env_file=root_env,
        env_example_file=tmp_path / ".env.example",
        legacy_env_file=legacy_env,
        now=lambda: _FIXED_NOW,
    )

    assert result.status == "not_needed"
    assert result.imported_keys == ()
    assert result.backup_path is None
    assert legacy_env.is_dir()
    assert not root_env.exists()


@pytest.mark.unit
def test_legacy_compose_env_migration_requires_root_env_to_be_regular_file(
    tmp_path: Path,
) -> None:
    """A root `.env` directory is reported before dotenv tries to parse it."""
    from awf.service.env_migration import migrate_legacy_compose_env_file

    root_env = tmp_path / ".env"
    legacy_env = tmp_path / "docker" / "compose" / ".env"
    root_env.mkdir()
    legacy_env.parent.mkdir(parents=True)
    legacy_env.write_text("AWF_API_TOKEN=legacy-token\n", encoding="utf-8")

    with pytest.raises(IsADirectoryError, match="Root env path is not a regular file"):
        migrate_legacy_compose_env_file(
            canonical_env_file=root_env,
            env_example_file=tmp_path / ".env.example",
            legacy_env_file=legacy_env,
            now=lambda: _FIXED_NOW,
        )

    assert legacy_env.exists()


@pytest.mark.unit
def test_legacy_compose_env_migration_ignores_env_example_directory(
    tmp_path: Path,
) -> None:
    """A template path must be a file before the migration reads it."""
    from awf.service.env_migration import migrate_legacy_compose_env_file

    root_env = tmp_path / ".env"
    env_example = tmp_path / ".env.example"
    legacy_env = tmp_path / "docker" / "compose" / ".env"
    env_example.mkdir()
    legacy_env.parent.mkdir(parents=True)
    legacy_env.write_text("AWF_API_TOKEN=legacy-token\n", encoding="utf-8")

    result = migrate_legacy_compose_env_file(
        canonical_env_file=root_env,
        env_example_file=env_example,
        legacy_env_file=legacy_env,
        now=lambda: _FIXED_NOW,
    )

    assert result.status == "migrated"
    assert result.created_env_file is True
    assert root_env.read_text(encoding="utf-8") == (
        "# Imported from legacy docker/compose/.env by AWF.\nAWF_API_TOKEN=legacy-token\n"
    )


@pytest.mark.unit
def test_legacy_compose_env_migration_escapes_dollar_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Migrated secrets with `${...}` survive later python-dotenv reads."""
    from dotenv import dotenv_values

    from awf.service.env_migration import migrate_legacy_compose_env_file

    monkeypatch.delenv("TOKEN_SUFFIX", raising=False)
    root_env = tmp_path / ".env"
    env_example = tmp_path / ".env.example"
    legacy_env = tmp_path / "docker" / "compose" / ".env"
    legacy_env.parent.mkdir(parents=True)
    env_example.write_text("AWF_API_TOKEN=\n", encoding="utf-8")
    legacy_env.write_text("AWF_API_TOKEN='secret-${TOKEN_SUFFIX}'\n", encoding="utf-8")

    result = migrate_legacy_compose_env_file(
        canonical_env_file=root_env,
        env_example_file=env_example,
        legacy_env_file=legacy_env,
        now=lambda: _FIXED_NOW,
    )

    assert result.status == "migrated"
    assert "AWF_API_TOKEN='secret-${TOKEN_SUFFIX}'" in root_env.read_text(encoding="utf-8")
    assert dotenv_values(root_env, interpolate=False)["AWF_API_TOKEN"] == ("secret-${TOKEN_SUFFIX}")


@pytest.mark.unit
def test_legacy_compose_env_migration_uses_compose_safe_quotes_for_dollar_single_quote(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Migrated values with `$` and `'` must not rely on `\'` in single quotes."""
    from awf.service.env_migration import migrate_legacy_compose_env_file
    from awf.service.environment import compose_env_file_values

    monkeypatch.setenv("er", "expanded")
    root_env = tmp_path / ".env"
    env_example = tmp_path / ".env.example"
    legacy_env = tmp_path / "docker" / "compose" / ".env"
    legacy_env.parent.mkdir(parents=True)
    env_example.write_text("AWF_API_TOKEN=\n", encoding="utf-8")
    legacy_env.write_text('AWF_API_TOKEN="sup\'$er"\n', encoding="utf-8")

    result = migrate_legacy_compose_env_file(
        canonical_env_file=root_env,
        env_example_file=env_example,
        legacy_env_file=legacy_env,
        now=lambda: _FIXED_NOW,
    )

    assert result.status == "migrated"
    root_text = root_env.read_text(encoding="utf-8")
    assert 'AWF_API_TOKEN="sup\'\\$er"' in root_text
    assert "AWF_API_TOKEN='sup\\'$er'" not in root_text
    assert compose_env_file_values(root_env)["AWF_API_TOKEN"] == "sup'$er"
