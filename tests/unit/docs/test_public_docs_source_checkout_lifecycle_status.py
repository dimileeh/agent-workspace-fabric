from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.unit.docs.public_docs_status_helpers import (
    DOTENV_DOUBLE_QUOTE_DECODE_FUNCTION_LINES,
    REPO_ROOT,
    _assert_source_checkout_service_env_restore_and_stop,
    _assert_source_checkout_stop_prefers_root_env,
    _markdown_fences,
    _markdown_section,
    _quickstart_upgrade_section,
    _required_index,
)


def test_source_checkout_stop_helper_allows_root_guard_without_legacy_when_optional() -> None:
    """Assert optional legacy fallback permits a guarded root-only stop."""
    section = "\n".join(
        (
            "if [ -f .env ]; then",
            "  docker compose --env-file .env -f docker/compose/local-service.yml stop",
            "else",
            "  docker compose -f docker/compose/local-service.yml stop",
            "fi",
        )
    )

    stop_start_index, stop_end_index = _assert_source_checkout_stop_prefers_root_env(
        "root-only guarded stop",
        section,
        0,
        require_legacy_fallback=False,
    )

    assert section[stop_start_index:].startswith("if [ -f .env ]; then")
    assert section[stop_end_index:].startswith("fi")


def test_source_checkout_stop_helper_requires_legacy_fallback_with_clear_message() -> None:
    """Assert required legacy fallback failures are explicit."""
    section = "\n".join(
        (
            "if [ -f .env ]; then",
            "  docker compose --env-file .env -f docker/compose/local-service.yml stop",
            "else",
            "  docker compose -f docker/compose/local-service.yml stop",
            "fi",
        )
    )

    with pytest.raises(AssertionError, match="must keep legacy compose env fallback"):
        _assert_source_checkout_stop_prefers_root_env(
            "root-only guarded stop",
            section,
            0,
            require_legacy_fallback=True,
        )


def test_source_checkout_upgrade_docs_refresh_persisted_metadata() -> None:
    """Assert source-checkout upgrades stop Core before refreshing source files."""
    quickstart_text = (REPO_ROOT / "docs" / "QUICKSTART.md").read_text(encoding="utf-8")
    upgrade_text = (REPO_ROOT / "docs" / "UPGRADE.md").read_text(encoding="utf-8")
    checkout_refresh_line = "git pull"
    cases = (
        (
            "Quickstart Lane 2",
            _quickstart_upgrade_section(
                quickstart_text,
                "## Lane 2: Source Checkout With Global Tool Install",
            ),
            "uv tool install . --force",
            'awf setup --source-checkout "$PWD"',
            'awf start --source-checkout "$PWD"',
        ),
        (
            "Quickstart Lane 3",
            _quickstart_upgrade_section(
                quickstart_text,
                "## Lane 3: Source Checkout With No Global Install",
            ),
            "uv sync --extra dev",
            'uv run --python 3.12 --extra dev awf setup --source-checkout "$PWD"',
            'uv run --python 3.12 --extra dev awf start --source-checkout "$PWD"',
        ),
        (
            "Upgrade source checkout with global tool install",
            _markdown_section(
                upgrade_text,
                "## Source Checkout With Global Tool Install",
            ),
            "uv tool install . --force",
            'awf setup --source-checkout "$PWD"',
            'awf start --source-checkout "$PWD"',
        ),
        (
            "Upgrade source checkout with no global install",
            _markdown_section(
                upgrade_text,
                "## Source Checkout With No Global Install",
            ),
            "uv sync --extra dev",
            'uv run --python 3.12 --extra dev awf setup --source-checkout "$PWD"',
            'uv run --python 3.12 --extra dev awf start --source-checkout "$PWD"',
        ),
    )

    for label, section, refresh_prereq, setup_line, start_line in cases:
        root_then_legacy_env_loop = "for env_file in .env docker/compose/.env; do"
        assert checkout_refresh_line in section, f"{label} is missing checkout refresh"
        assert refresh_prereq in section, f"{label} is missing upgrade prerequisite"
        assert section.count(root_then_legacy_env_loop) == 4, (
            f"{label} must restore API token, Postgres password, Postgres host port, and database URL "
            "from root .env before legacy docker/compose/.env"
        )
        (
            env_restore_start_index,
            env_restore_end_index,
            _stop_start_index,
            stop_end_index,
        ) = _assert_source_checkout_service_env_restore_and_stop(
            label,
            section,
            "upgrading",
            require_database_url_restore=True,
            require_legacy_fallback=True,
        )
        assert setup_line in section, f"{label} does not refresh source_checkout metadata"
        assert start_line in section, f"{label} is missing source-checkout start"
        checkout_refresh_index = _required_index(section, checkout_refresh_line, label)
        refresh_prereq_index = _required_index(section, refresh_prereq, label)
        setup_index = _required_index(section, setup_line, label)
        start_index = _required_index(section, start_line, label)
        assert (
            env_restore_start_index
            < env_restore_end_index
            < stop_end_index
            < checkout_refresh_index
            < refresh_prereq_index
            < setup_index
            < start_index
        ), f"{label} must stop Core before refreshing source files"


def test_source_checkout_upgrade_env_restore_exports_persisted_database_url_over_stale_shell(
    tmp_path: Path,
) -> None:
    """Assert source-checkout upgrades restore persisted database URLs."""
    persisted_database_url = "postgresql+asyncpg://awf:p%40ss%22quote%5Ctail@localhost:15433/awf"
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                'AWF_API_TOKEN="tok\\$en"',
                'AWF_POSTGRES_PASSWORD="p@ss\\"quote\\\\tail"',
                f'AWF_DATABASE_URL="{persisted_database_url}"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    quickstart_text = (REPO_ROOT / "docs" / "QUICKSTART.md").read_text(encoding="utf-8")
    upgrade_text = (REPO_ROOT / "docs" / "UPGRADE.md").read_text(encoding="utf-8")
    cases = (
        (
            "Quickstart Lane 2",
            "docs/QUICKSTART.md",
            _quickstart_upgrade_section(
                quickstart_text,
                "## Lane 2: Source Checkout With Global Tool Install",
            ),
        ),
        (
            "Quickstart Lane 3",
            "docs/QUICKSTART.md",
            _quickstart_upgrade_section(
                quickstart_text,
                "## Lane 3: Source Checkout With No Global Install",
            ),
        ),
        (
            "Upgrade source checkout with global tool install",
            "docs/UPGRADE.md",
            _markdown_section(upgrade_text, "## Source Checkout With Global Tool Install"),
        ),
        (
            "Upgrade source checkout with no global install",
            "docs/UPGRADE.md",
            _markdown_section(upgrade_text, "## Source Checkout With No Global Install"),
        ),
    )
    stale_env = {
        **os.environ,
        "AWF_API_TOKEN": "stale-token-from-shell",
        "AWF_POSTGRES_PASSWORD": "stale-password-from-shell",
        "AWF_DATABASE_URL": "postgresql+asyncpg://awf:stale@localhost:5433/awf",
    }

    for label, path, section in cases:
        bash_fences = [
            fence for fence in _markdown_fences(path, section) if fence.language == "bash"
        ]
        assert len(bash_fences) == 1, label
        body = bash_fences[0].body
        restore_start = _required_index(
            body,
            DOTENV_DOUBLE_QUOTE_DECODE_FUNCTION_LINES[0],
            label,
        )
        guarded_stop_index = body.find("if [ -f .env ]; then", restore_start)
        bare_stop_index = body.find(
            "docker compose --env-file .env -f docker/compose/local-service.yml stop",
            restore_start,
        )
        stop_start_candidates = [
            index for index in (guarded_stop_index, bare_stop_index) if index != -1
        ]
        assert stop_start_candidates, label
        restore_script = body[restore_start : min(stop_start_candidates)]
        script = "\n".join(
            (
                restore_script,
                (
                    'printf "%s\\n%s\\n%s\\n" "$AWF_API_TOKEN" '
                    '"$AWF_POSTGRES_PASSWORD" "$AWF_DATABASE_URL"'
                ),
            )
        )

        result = subprocess.run(  # noqa: S602
            ["bash", "-c", script],
            cwd=tmp_path,
            env=stale_env,
            check=False,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"{label}: {result.stderr}"
        assert result.stdout == f'tok$en\np@ss"quote\\tail\n{persisted_database_url}\n', label


def test_source_checkout_upgrade_without_persisted_database_url_drops_stale_shell_url(
    tmp_path: Path,
) -> None:
    """Assert legacy source env files without AWF_DATABASE_URL do not keep stale URLs."""
    legacy_env_file = tmp_path / "docker" / "compose" / ".env"
    legacy_env_file.parent.mkdir(parents=True)
    legacy_env_file.write_text(
        "\n".join(
            [
                "AWF_API_TOKEN=legacy-token",
                "AWF_POSTGRES_PASSWORD=awf_dev",
                "AWF_POSTGRES_HOST_PORT=15433",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    quickstart_text = (REPO_ROOT / "docs" / "QUICKSTART.md").read_text(encoding="utf-8")
    upgrade_text = (REPO_ROOT / "docs" / "UPGRADE.md").read_text(encoding="utf-8")
    cases = (
        (
            "Quickstart Lane 2",
            "docs/QUICKSTART.md",
            _quickstart_upgrade_section(
                quickstart_text,
                "## Lane 2: Source Checkout With Global Tool Install",
            ),
        ),
        (
            "Quickstart Lane 3",
            "docs/QUICKSTART.md",
            _quickstart_upgrade_section(
                quickstart_text,
                "## Lane 3: Source Checkout With No Global Install",
            ),
        ),
        (
            "Upgrade source checkout with global tool install",
            "docs/UPGRADE.md",
            _markdown_section(upgrade_text, "## Source Checkout With Global Tool Install"),
        ),
        (
            "Upgrade source checkout with no global install",
            "docs/UPGRADE.md",
            _markdown_section(upgrade_text, "## Source Checkout With No Global Install"),
        ),
    )

    for label, path, section in cases:
        bash_fences = [
            fence for fence in _markdown_fences(path, section) if fence.language == "bash"
        ]
        assert len(bash_fences) == 1, label
        body = bash_fences[0].body
        restore_start = _required_index(
            body,
            DOTENV_DOUBLE_QUOTE_DECODE_FUNCTION_LINES[0],
            label,
        )
        stop_start = _required_index(body, "if [ -f .env ]; then", label, restore_start)
        stale_env = {
            **os.environ,
            "AWF_DATABASE_URL": "postgresql+asyncpg://awf:stale@localhost:5433/awf",
        }
        script = "\n".join(
            (
                ("unset AWF_API_TOKEN AWF_POSTGRES_PASSWORD AWF_POSTGRES_HOST_PORT"),
                body[restore_start:stop_start],
                (
                    'printf "%s\\n%s\\n%s\\n%s\\n" "$AWF_API_TOKEN" '
                    '"$AWF_POSTGRES_PASSWORD" "${AWF_POSTGRES_HOST_PORT:-}" '
                    '"${AWF_DATABASE_URL-}"'
                ),
            )
        )

        result = subprocess.run(  # noqa: S602
            ["bash", "-c", script],
            cwd=tmp_path,
            check=False,
            capture_output=True,
            env=stale_env,
            text=True,
        )

        assert result.returncode == 0, f"{label}: {result.stderr}"
        assert result.stdout == "legacy-token\nawf_dev\n15433\n\n", label
