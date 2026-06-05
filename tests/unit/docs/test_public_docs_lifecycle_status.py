from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest

from tests.unit.docs.public_docs_status_helpers import (
    DOTENV_DOUBLE_QUOTE_DECODE_FUNCTION_LINES,
    DOTENV_UNQUOTED_INLINE_COMMENT_STRIP_FUNCTION_LINES,
    PACKAGE_ENV_INLINE_COMMENT_STRIP_LINES,
    PACKAGE_ENV_QUOTE_STRIP_LINES,
    PACKAGE_ENV_READ_LINES,
    REPO_ROOT,
    SOURCE_CHECKOUT_ENV_QUOTE_STRIP_LINES,
    _assert_package_upgrade_restores_service_env,
    _assert_source_checkout_service_env_restore_and_stop,
    _assert_source_checkout_stop_prefers_root_env,
    _markdown_fences,
    _markdown_section,
    _markdown_section_between,
    _package_env_restore_script,
    _quickstart_upgrade_section,
    _required_index,
)


def _package_database_url_restore_lines(lifecycle: str = "upgrading") -> tuple[str, ...]:
    """Return the package-lane database URL restore block used by negative fixtures."""
    return (
        PACKAGE_ENV_READ_LINES["AWF_DATABASE_URL"],
        PACKAGE_ENV_INLINE_COMMENT_STRIP_LINES["AWF_DATABASE_URL"],
        *PACKAGE_ENV_QUOTE_STRIP_LINES["AWF_DATABASE_URL"],
        'if [ -n "$AWF_PERSISTED_DATABASE_URL" ]; then',
        '  export AWF_DATABASE_URL="$AWF_PERSISTED_DATABASE_URL"',
        "else",
        (
            '  : "${AWF_DATABASE_URL:?restore the AWF_DATABASE_URL used for '
            f'the running local Core or persist it in .env before {lifecycle}}}"'
        ),
        "  export AWF_DATABASE_URL",
        "fi",
    )


def _required_rollback_subsection(
    rollback_section: str,
    start_marker: str,
    *,
    end_marker: str | None = None,
) -> str:
    """Return rollback text bounded by required markers with clear failures."""
    _, found_start_marker, after_start_marker = rollback_section.partition(start_marker)
    assert found_start_marker, f"Rollback section marker {start_marker!r} not found"
    if end_marker is None:
        return after_start_marker

    subsection, found_end_marker, _ = after_start_marker.partition(end_marker)
    assert found_end_marker, (
        f"Rollback section marker {end_marker!r} not found after {start_marker!r}"
    )
    return subsection


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


def test_package_upgrade_docs_restore_service_env_before_start() -> None:
    """Assert package upgrades keep mandatory local service env available."""
    quickstart_text = (REPO_ROOT / "docs" / "QUICKSTART.md").read_text(encoding="utf-8")
    upgrade_text = (REPO_ROOT / "docs" / "UPGRADE.md").read_text(encoding="utf-8")
    cases = (
        (
            "Quickstart Lane 1",
            _quickstart_upgrade_section(quickstart_text, "## Lane 1: uv tool or pipx"),
            "pipx upgrade agent-workspace-fabric",
        ),
        (
            "Upgrade uv tool",
            _markdown_section(upgrade_text, "## uv tool"),
            "uv tool upgrade agent-workspace-fabric",
        ),
        (
            "Upgrade pipx",
            _markdown_section(upgrade_text, "## pipx"),
            "pipx upgrade agent-workspace-fabric",
        ),
        (
            "Upgrade virtualenv / pip",
            _markdown_section(upgrade_text, "## Virtualenv / pip"),
            "pip install --upgrade agent-workspace-fabric",
        ),
    )

    for label, section, upgrade_line in cases:
        _assert_package_upgrade_restores_service_env(label, section, upgrade_line)


def test_package_upgrade_env_restore_exports_persisted_dotenv_over_stale_shell(
    tmp_path: Path,
) -> None:
    """Assert package upgrade snippets export persisted service env before restart."""
    env_file = tmp_path / ".env"
    persisted_database_url = "postgresql+asyncpg://awf:p%40ss%22quote%5Ctail@localhost:15433/awf"
    env_file.write_text(
        "\n".join(
            [
                '  export AWF_API_TOKEN="tok\\$en"',
                'export AWF_POSTGRES_PASSWORD="p@ss\\"quote\\\\tail"',
                f'AWF_DATABASE_URL="{persisted_database_url}"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    upgrade_text = (REPO_ROOT / "docs" / "UPGRADE.md").read_text(encoding="utf-8")
    quickstart_text = (REPO_ROOT / "docs" / "QUICKSTART.md").read_text(encoding="utf-8")
    rollback_section = _markdown_section(upgrade_text, "## Rollback")
    release_rollback_section = _required_rollback_subsection(
        rollback_section,
        "For release-installed lanes",
        end_marker="For the source checkout with global tool install lane",
    )
    cases = (
        (
            "Quickstart Lane 1",
            _quickstart_upgrade_section(quickstart_text, "## Lane 1: uv tool or pipx"),
        ),
        ("Upgrade uv tool", _markdown_section(upgrade_text, "## uv tool")),
        ("Upgrade pipx", _markdown_section(upgrade_text, "## pipx")),
        ("Upgrade virtualenv / pip", _markdown_section(upgrade_text, "## Virtualenv / pip")),
        ("Release-installed rollback", release_rollback_section),
    )

    stale_env = {
        **os.environ,
        "AWF_API_TOKEN": "stale-token-from-shell",
        "AWF_POSTGRES_PASSWORD": "stale-password-from-shell",
        "AWF_DATABASE_URL": "postgresql+asyncpg://awf:stale@localhost:5433/awf",
    }

    for label, section in cases:
        script = "\n".join(
            (
                _package_env_restore_script(section, label),
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


def test_lifecycle_env_restore_uses_last_dotenv_assignment(tmp_path: Path) -> None:
    """Assert documented restore snippets match Compose duplicate-key precedence."""
    persisted_database_url = "postgresql+asyncpg://awf:last@localhost:15433/awf"
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "AWF_API_TOKEN=stale-token",
                "export AWF_API_TOKEN=last-token",
                "AWF_POSTGRES_PASSWORD=stale-password",
                "export AWF_POSTGRES_PASSWORD=last-password",
                "AWF_POSTGRES_HOST_PORT=5433",
                "export AWF_POSTGRES_HOST_PORT=15433",
                "AWF_DATABASE_URL=postgresql+asyncpg://awf:stale@localhost:5433/awf",
                f"export AWF_DATABASE_URL={persisted_database_url}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    quickstart_text = (REPO_ROOT / "docs" / "QUICKSTART.md").read_text(encoding="utf-8")
    upgrade_text = (REPO_ROOT / "docs" / "UPGRADE.md").read_text(encoding="utf-8")
    rollback_section = _markdown_section(upgrade_text, "## Rollback")
    global_rollback_heading = "For the source checkout with global tool install lane"
    no_global_rollback_heading = "For the source checkout with no global install lane"
    release_rollback_section = _required_rollback_subsection(
        rollback_section,
        "For release-installed lanes",
        end_marker=global_rollback_heading,
    )

    package_cases = (
        (
            "Quickstart Lane 1",
            _quickstart_upgrade_section(quickstart_text, "## Lane 1: uv tool or pipx"),
        ),
        ("Upgrade uv tool", _markdown_section(upgrade_text, "## uv tool")),
        ("Upgrade pipx", _markdown_section(upgrade_text, "## pipx")),
        ("Upgrade virtualenv / pip", _markdown_section(upgrade_text, "## Virtualenv / pip")),
        ("Release-installed rollback", release_rollback_section),
    )

    for label, section in package_cases:
        script = "\n".join(
            (
                "unset AWF_API_TOKEN AWF_POSTGRES_PASSWORD AWF_DATABASE_URL",
                _package_env_restore_script(section, label),
                (
                    'printf "%s\\n%s\\n%s\\n" "$AWF_API_TOKEN" '
                    '"$AWF_POSTGRES_PASSWORD" "$AWF_DATABASE_URL"'
                ),
            )
        )

        result = subprocess.run(  # noqa: S602
            ["bash", "-c", script],
            cwd=tmp_path,
            check=False,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"{label}: {result.stderr}"
        assert result.stdout == f"last-token\nlast-password\n{persisted_database_url}\n", label

    source_cases = (
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

    for label, path, section in source_cases:
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
        script = "\n".join(
            (
                (
                    "unset AWF_API_TOKEN AWF_POSTGRES_PASSWORD "
                    "AWF_POSTGRES_HOST_PORT AWF_DATABASE_URL"
                ),
                body[restore_start:stop_start],
                (
                    'printf "%s\\n%s\\n%s\\n%s\\n" "$AWF_API_TOKEN" '
                    '"$AWF_POSTGRES_PASSWORD" "$AWF_POSTGRES_HOST_PORT" '
                    '"$AWF_DATABASE_URL"'
                ),
            )
        )

        result = subprocess.run(  # noqa: S602
            ["bash", "-c", script],
            cwd=tmp_path,
            check=False,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"{label}: {result.stderr}"
        assert result.stdout == (f"last-token\nlast-password\n15433\n{persisted_database_url}\n"), (
            label
        )

    source_rollback_cases = (
        (
            "Global source-checkout rollback",
            _required_rollback_subsection(
                rollback_section,
                global_rollback_heading,
                end_marker=no_global_rollback_heading,
            ),
        ),
        (
            "No-global source-checkout rollback",
            _required_rollback_subsection(
                rollback_section,
                no_global_rollback_heading,
            ),
        ),
    )

    for label, section in source_rollback_cases:
        bash_fences = [
            fence
            for fence in _markdown_fences("docs/UPGRADE.md", section)
            if fence.language == "bash"
        ]
        assert len(bash_fences) == 1, label
        body = bash_fences[0].body
        restore_start = _required_index(
            body,
            DOTENV_DOUBLE_QUOTE_DECODE_FUNCTION_LINES[0],
            label,
        )
        stop_start = _required_index(body, "if [ -f .env ]; then", label, restore_start)
        script = "\n".join(
            (
                "unset AWF_API_TOKEN AWF_POSTGRES_PASSWORD",
                body[restore_start:stop_start],
                'printf "%s\\n%s\\n" "$AWF_API_TOKEN" "$AWF_POSTGRES_PASSWORD"',
            )
        )

        result = subprocess.run(  # noqa: S602
            ["bash", "-c", script],
            cwd=tmp_path,
            check=False,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"{label}: {result.stderr}"
        assert result.stdout == "last-token\nlast-password\n", label


def test_upgrade_env_restore_strips_unquoted_inline_dotenv_comments(
    tmp_path: Path,
) -> None:
    """Assert upgrade snippets restore the same unquoted dotenv bytes as Compose."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "AWF_API_TOKEN=token-from-env # local token",
                "AWF_POSTGRES_PASSWORD=awf_dev # local password",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    upgrade_text = (REPO_ROOT / "docs" / "UPGRADE.md").read_text(encoding="utf-8")
    quickstart_text = (REPO_ROOT / "docs" / "QUICKSTART.md").read_text(encoding="utf-8")
    rollback_section = _markdown_section(upgrade_text, "## Rollback")
    global_rollback_heading = "For the source checkout with global tool install lane"
    no_global_rollback_heading = "For the source checkout with no global install lane"
    release_rollback_section = _required_rollback_subsection(
        rollback_section,
        "For release-installed lanes",
        end_marker=global_rollback_heading,
    )
    package_cases = (
        (
            "Quickstart Lane 1",
            _quickstart_upgrade_section(quickstart_text, "## Lane 1: uv tool or pipx"),
        ),
        ("Upgrade uv tool", _markdown_section(upgrade_text, "## uv tool")),
        ("Upgrade pipx", _markdown_section(upgrade_text, "## pipx")),
        ("Upgrade virtualenv / pip", _markdown_section(upgrade_text, "## Virtualenv / pip")),
        ("Release-installed rollback", release_rollback_section),
    )
    source_cases = (
        (
            "Upgrade source checkout with global tool install",
            _markdown_section(upgrade_text, "## Source Checkout With Global Tool Install"),
        ),
        (
            "Upgrade source checkout with no global install",
            _markdown_section(upgrade_text, "## Source Checkout With No Global Install"),
        ),
        (
            "Global source-checkout rollback",
            _required_rollback_subsection(
                rollback_section,
                global_rollback_heading,
                end_marker=no_global_rollback_heading,
            ),
        ),
        (
            "No-global source-checkout rollback",
            _required_rollback_subsection(
                rollback_section,
                no_global_rollback_heading,
            ),
        ),
    )

    scripts: list[tuple[str, str]] = [
        (label, _package_env_restore_script(section, label)) for label, section in package_cases
    ]
    for label, section in source_cases:
        bash_fences = [
            fence
            for fence in _markdown_fences("docs/UPGRADE.md", section)
            if fence.language == "bash"
        ]
        assert len(bash_fences) == 1, label
        body = bash_fences[0].body
        restore_start = _required_index(
            body,
            DOTENV_DOUBLE_QUOTE_DECODE_FUNCTION_LINES[0],
            label,
        )
        stop_start = _required_index(body, "if [ -f .env ]; then", label)
        scripts.append((label, body[restore_start:stop_start]))

    for label, restore_script in scripts:
        script = "\n".join(
            (
                "unset AWF_API_TOKEN AWF_POSTGRES_PASSWORD",
                restore_script,
                'printf "%s\\n%s\\n" "$AWF_API_TOKEN" "$AWF_POSTGRES_PASSWORD"',
            )
        )
        result = subprocess.run(  # noqa: S602
            ["bash", "-c", script],
            cwd=tmp_path,
            check=False,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"{label}: {result.stderr}"
        assert result.stdout == "token-from-env\nawf_dev\n", label


def test_upgrade_env_restore_strips_quoted_inline_dotenv_comments(
    tmp_path: Path,
) -> None:
    """Assert upgrade snippets restore quoted dotenv values with trailing comments."""
    env_file = tmp_path / ".env"
    persisted_database_url = "postgresql+asyncpg://awf:p%23w@localhost:15433/awf"
    env_file.write_text(
        "\n".join(
            [
                'AWF_API_TOKEN="tok\\"\\$en # inside" # local token',
                "AWF_POSTGRES_PASSWORD='pw # inside' # local password",
                f'AWF_DATABASE_URL="{persisted_database_url}" # local database',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    upgrade_text = (REPO_ROOT / "docs" / "UPGRADE.md").read_text(encoding="utf-8")
    rollback_section = _markdown_section(upgrade_text, "## Rollback")
    global_rollback_heading = "For the source checkout with global tool install lane"
    no_global_rollback_heading = "For the source checkout with no global install lane"
    release_rollback_section = _required_rollback_subsection(
        rollback_section,
        "For release-installed lanes",
        end_marker=global_rollback_heading,
    )
    package_cases = (
        ("Upgrade uv tool", _markdown_section(upgrade_text, "## uv tool")),
        ("Upgrade pipx", _markdown_section(upgrade_text, "## pipx")),
        ("Upgrade virtualenv / pip", _markdown_section(upgrade_text, "## Virtualenv / pip")),
        ("Release-installed rollback", release_rollback_section),
    )
    source_cases = (
        (
            "Upgrade source checkout with global tool install",
            _markdown_section(upgrade_text, "## Source Checkout With Global Tool Install"),
        ),
        (
            "Upgrade source checkout with no global install",
            _markdown_section(upgrade_text, "## Source Checkout With No Global Install"),
        ),
        (
            "Global source-checkout rollback",
            _required_rollback_subsection(
                rollback_section,
                global_rollback_heading,
                end_marker=no_global_rollback_heading,
            ),
        ),
        (
            "No-global source-checkout rollback",
            _required_rollback_subsection(
                rollback_section,
                no_global_rollback_heading,
            ),
        ),
    )

    scripts: list[tuple[str, str, bool]] = [
        (label, _package_env_restore_script(section, label), True)
        for label, section in package_cases
    ]
    for label, section in source_cases:
        bash_fences = [
            fence
            for fence in _markdown_fences("docs/UPGRADE.md", section)
            if fence.language == "bash"
        ]
        assert len(bash_fences) == 1, label
        body = bash_fences[0].body
        restore_start = _required_index(
            body,
            DOTENV_DOUBLE_QUOTE_DECODE_FUNCTION_LINES[0],
            label,
        )
        stop_start = _required_index(body, "if [ -f .env ]; then", label)
        restore_script = body[restore_start:stop_start]
        scripts.append((label, restore_script, "AWF_PERSISTED_DATABASE_URL" in restore_script))

    for label, restore_script, restores_database_url in scripts:
        if restores_database_url:
            print_command = (
                'printf "%s\\n%s\\n%s\\n" "$AWF_API_TOKEN" '
                '"$AWF_POSTGRES_PASSWORD" "$AWF_DATABASE_URL"'
            )
            expected_stdout = f'tok"$en # inside\npw # inside\n{persisted_database_url}\n'
        else:
            print_command = 'printf "%s\\n%s\\n" "$AWF_API_TOKEN" "$AWF_POSTGRES_PASSWORD"'
            expected_stdout = 'tok"$en # inside\npw # inside\n'
        script = "\n".join(
            (
                "unset AWF_API_TOKEN AWF_POSTGRES_PASSWORD AWF_DATABASE_URL",
                restore_script,
                print_command,
            )
        )

        result = subprocess.run(  # noqa: S602
            ["bash", "-c", script],
            cwd=tmp_path,
            check=False,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"{label}: {result.stderr}"
        assert result.stdout == expected_stdout, label


def test_quickstart_and_uninstall_restore_strip_quoted_inline_dotenv_comments(
    tmp_path: Path,
) -> None:
    """Assert Quickstart and uninstall snippets match Compose quoted comments."""
    env_file = tmp_path / ".env"
    persisted_database_url = "postgresql+asyncpg://awf:p%23w@localhost:15433/awf"
    env_file.write_text(
        "\n".join(
            [
                'AWF_API_TOKEN="tok\\"\\$en # inside" # local token',
                "AWF_POSTGRES_PASSWORD='pw # inside' # local password",
                f'AWF_DATABASE_URL="{persisted_database_url}" # local database',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    quickstart_text = (REPO_ROOT / "docs" / "QUICKSTART.md").read_text(encoding="utf-8")
    uninstall_text = (REPO_ROOT / "docs" / "UNINSTALL.md").read_text(encoding="utf-8")

    scripts: list[tuple[str, str, bool]] = [
        (
            "Quickstart Lane 1 package upgrade",
            _package_env_restore_script(
                _quickstart_upgrade_section(quickstart_text, "## Lane 1: uv tool or pipx"),
                "Quickstart Lane 1 package upgrade",
            ),
            True,
        )
    ]
    for heading in (
        "## Lane 2: Source Checkout With Global Tool Install",
        "## Lane 3: Source Checkout With No Global Install",
    ):
        upgrade_section = _quickstart_upgrade_section(quickstart_text, heading)
        upgrade_fences = [
            fence
            for fence in _markdown_fences("docs/QUICKSTART.md", upgrade_section)
            if fence.language == "bash"
        ]
        assert len(upgrade_fences) == 1, heading
        scripts.append(
            (
                f"{heading} upgrade",
                upgrade_fences[0].body.split("if [ -f .env ]; then", maxsplit=1)[0],
                True,
            )
        )

        lane_section = _markdown_section(quickstart_text, heading)
        uninstall_fences = [
            fence
            for fence in _markdown_fences("docs/QUICKSTART.md", lane_section)
            if fence.language == "bash"
            and 'AWF_PERSISTED_API_TOKEN=""' in fence.body
            and "/path/to/replacement/aira-agent-workspace-fabric" in fence.body
        ]
        assert len(uninstall_fences) == 1, heading
        restore_start = _required_index(
            uninstall_fences[0].body,
            DOTENV_DOUBLE_QUOTE_DECODE_FUNCTION_LINES[0],
            f"{heading} uninstall",
        )
        scripts.append(
            (
                f"{heading} uninstall",
                uninstall_fences[0]
                .body[restore_start:]
                .split("if [ -f .env ]; then", maxsplit=1)[0],
                False,
            )
        )

    uninstall_cases = (
        (
            "Uninstall intro source-checkout refresh",
            uninstall_text.split("## uv tool", maxsplit=1)[0],
        ),
        (
            "Uninstall global source-checkout refresh",
            _markdown_section(uninstall_text, "## Source Checkout With Global Tool Install"),
        ),
        (
            "Uninstall no-global source-checkout refresh",
            _markdown_section(uninstall_text, "## Source Checkout With No Global Install"),
        ),
    )
    for label, section in uninstall_cases:
        bash_fences = [
            fence
            for fence in _markdown_fences("docs/UNINSTALL.md", section)
            if fence.language == "bash" and 'AWF_PERSISTED_API_TOKEN=""' in fence.body
        ]
        assert len(bash_fences) == 1, label
        restore_start = _required_index(
            bash_fences[0].body,
            DOTENV_DOUBLE_QUOTE_DECODE_FUNCTION_LINES[0],
            label,
        )
        scripts.append(
            (
                label,
                bash_fences[0].body[restore_start:].split("if [ -f .env ]; then", maxsplit=1)[0],
                False,
            )
        )

    for label, restore_script, restores_database_url in scripts:
        if restores_database_url:
            print_command = (
                'printf "%s\\n%s\\n%s\\n" "$AWF_API_TOKEN" '
                '"$AWF_POSTGRES_PASSWORD" "$AWF_DATABASE_URL"'
            )
            expected_stdout = f'tok"$en # inside\npw # inside\n{persisted_database_url}\n'
        else:
            print_command = 'printf "%s\\n%s\\n" "$AWF_API_TOKEN" "$AWF_POSTGRES_PASSWORD"'
            expected_stdout = 'tok"$en # inside\npw # inside\n'
        script = "\n".join(
            (
                "unset AWF_API_TOKEN AWF_POSTGRES_PASSWORD AWF_DATABASE_URL",
                restore_script,
                print_command,
            )
        )

        result = subprocess.run(  # noqa: S602
            ["bash", "-c", script],
            cwd=tmp_path,
            check=False,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"{label}: {result.stderr}"
        assert result.stdout == expected_stdout, label


@pytest.mark.parametrize(
    "heading",
    (
        "## Lane 2: Source Checkout With Global Tool Install",
        "## Lane 3: Source Checkout With No Global Install",
    ),
)
def test_quickstart_source_checkout_upgrade_env_restore_strips_unquoted_inline_dotenv_comments(
    heading: str,
    tmp_path: Path,
) -> None:
    """Assert Quickstart source upgrades restore the same unquoted dotenv bytes as Compose."""
    env_file = tmp_path / ".env"
    persisted_database_url = "postgresql+asyncpg://awf:awf_dev@localhost:5432/awf"
    env_file.write_text(
        "\n".join(
            [
                "AWF_API_TOKEN=token-from-env # local token",
                "AWF_POSTGRES_PASSWORD=awf_dev # local password",
                f"AWF_DATABASE_URL={persisted_database_url} # local database",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    quickstart_text = (REPO_ROOT / "docs" / "QUICKSTART.md").read_text(encoding="utf-8")
    upgrade_section = _quickstart_upgrade_section(quickstart_text, heading)
    bash_fences = [
        fence
        for fence in _markdown_fences("docs/QUICKSTART.md", upgrade_section)
        if fence.language == "bash"
    ]
    assert len(bash_fences) == 1
    restore_script = bash_fences[0].body.split("if [ -f .env ]; then", maxsplit=1)[0]
    script = "\n".join(
        (
            "unset AWF_API_TOKEN AWF_POSTGRES_PASSWORD AWF_DATABASE_URL",
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
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, f"{heading}: {result.stderr}"
    assert result.stdout == f"token-from-env\nawf_dev\n{persisted_database_url}\n", heading


def test_uninstall_source_checkout_env_restore_strips_unquoted_inline_dotenv_comments(
    tmp_path: Path,
) -> None:
    """Assert uninstall refresh snippets restore the same unquoted dotenv bytes as Compose."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "AWF_API_TOKEN=token-from-env # local token",
                "AWF_POSTGRES_PASSWORD=awf_dev # local password",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    uninstall_text = (REPO_ROOT / "docs" / "UNINSTALL.md").read_text(encoding="utf-8")
    cases = (
        (
            "Intro source-checkout uninstall",
            uninstall_text.split("## uv tool", maxsplit=1)[0],
        ),
        (
            "Global source-checkout uninstall",
            _markdown_section(uninstall_text, "## Source Checkout With Global Tool Install"),
        ),
        (
            "No-global source-checkout uninstall",
            _markdown_section(uninstall_text, "## Source Checkout With No Global Install"),
        ),
    )

    for label, section in cases:
        bash_fences = [
            fence
            for fence in _markdown_fences("docs/UNINSTALL.md", section)
            if fence.language == "bash" and 'AWF_PERSISTED_API_TOKEN=""' in fence.body
        ]
        assert len(bash_fences) == 1, label
        restore_body = bash_fences[0].body
        restore_start = _required_index(
            restore_body,
            "awf_decode_double_quoted_dotenv() {",
            label,
        )
        restore_script = restore_body[restore_start:].split("if [ -f .env ]; then", maxsplit=1)[0]
        script = "\n".join(
            (
                "unset AWF_API_TOKEN AWF_POSTGRES_PASSWORD",
                restore_script,
                'printf "%s\\n%s\\n" "$AWF_API_TOKEN" "$AWF_POSTGRES_PASSWORD"',
            )
        )

        result = subprocess.run(  # noqa: S602
            ["bash", "-c", script],
            cwd=tmp_path,
            check=False,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"{label}: {result.stderr}"
        assert result.stdout == "token-from-env\nawf_dev\n", label


@pytest.mark.parametrize("doc_name", ("QUICKSTART.md", "UNINSTALL.md", "UPGRADE.md"))
def test_source_checkout_env_restore_decodes_quoted_dotenv_entries(
    doc_name: str,
    tmp_path: Path,
) -> None:
    """Assert source-checkout snippets do not export raw dotenv escapes as secret bytes."""
    doc_text = (REPO_ROOT / "docs" / doc_name).read_text(encoding="utf-8")
    env_file = tmp_path / ".env"

    for key, persisted_var in (
        ("AWF_API_TOKEN", "AWF_PERSISTED_API_TOKEN"),
        ("AWF_POSTGRES_PASSWORD", "AWF_PERSISTED_POSTGRES_PASSWORD"),
    ):
        expressions = set(re.findall(rf"sed -n '([^']*{key}[^']*)'", doc_text))
        assert len(expressions) == 1, (
            f"Expected one unique sed expression for {key!r} in {doc_name}, "
            f"found: {sorted(expressions)!r}"
        )
        expression = expressions.pop()
        script = "\n".join(
            [
                *DOTENV_DOUBLE_QUOTE_DECODE_FUNCTION_LINES,
                'env_file="$1"',
                f'{persisted_var}="$(sed -n {shlex.quote(expression)} "$env_file" | tail -n 1)"',
                *SOURCE_CHECKOUT_ENV_QUOTE_STRIP_LINES[key],
                f'printf "%s\\n" "${{{persisted_var}}}"',
            ]
        )

        for raw_value, expected_value in (
            ('"from\\$double"', "from$double"),
            ('"slash\\\\quote\\""', 'slash\\quote"'),
            ("'from-single-quotes'", "from-single-quotes"),
        ):
            env_file.write_text(
                f"{key}_BACKUP=keep\nexport {key}={raw_value}\n",
                encoding="utf-8",
            )

            result = subprocess.run(  # noqa: S602
                ["bash", "-c", script, "bash", str(env_file)],
                check=True,
                capture_output=True,
                text=True,
            )

            assert result.stdout == expected_value + "\n"


@pytest.mark.parametrize(
    "heading",
    (
        "## Lane 2: Source Checkout With Global Tool Install",
        "## Lane 3: Source Checkout With No Global Install",
    ),
)
def test_quickstart_source_checkout_upgrade_accepts_default_api_token(
    heading: str,
    tmp_path: Path,
) -> None:
    """Assert copied example `.env` source upgrades keep the local default token."""
    quickstart_text = (REPO_ROOT / "docs" / "QUICKSTART.md").read_text(encoding="utf-8")
    upgrade_section = _quickstart_upgrade_section(quickstart_text, heading)
    bash_fences = [
        fence
        for fence in _markdown_fences("docs/QUICKSTART.md", upgrade_section)
        if fence.language == "bash"
    ]
    assert len(bash_fences) == 1
    token_restore_script = bash_fences[0].body.split(
        'AWF_PERSISTED_POSTGRES_PASSWORD=""',
        maxsplit=1,
    )[0]
    env_file = tmp_path / ".env"
    env_file.write_text("AWF_API_TOKEN=\n", encoding="utf-8")
    script = "\n".join(
        (
            "unset AWF_API_TOKEN",
            token_restore_script,
            'printf "%s\\n" "$AWF_API_TOKEN"',
        )
    )

    result = subprocess.run(  # noqa: S602
        ["bash", "-c", script],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "local-dev-token\n"


def test_upgrade_source_checkout_restore_accepts_default_api_token(tmp_path: Path) -> None:
    """Assert source upgrade/rollback snippets keep the local default token."""
    upgrade_text = (REPO_ROOT / "docs" / "UPGRADE.md").read_text(encoding="utf-8")
    rollback_section = _markdown_section(upgrade_text, "## Rollback")
    global_rollback_heading = "For the source checkout with global tool install lane"
    no_global_rollback_heading = "For the source checkout with no global install lane"
    cases = (
        (
            "Upgrade source checkout with global tool install",
            _markdown_section(upgrade_text, "## Source Checkout With Global Tool Install"),
        ),
        (
            "Upgrade source checkout with no global install",
            _markdown_section(upgrade_text, "## Source Checkout With No Global Install"),
        ),
        (
            "Global source-checkout rollback",
            _required_rollback_subsection(
                rollback_section,
                global_rollback_heading,
                end_marker=no_global_rollback_heading,
            ),
        ),
        (
            "No-global source-checkout rollback",
            _required_rollback_subsection(
                rollback_section,
                no_global_rollback_heading,
            ),
        ),
    )

    for label, section in cases:
        bash_fences = [
            fence
            for fence in _markdown_fences("docs/UPGRADE.md", section)
            if fence.language == "bash"
        ]
        assert len(bash_fences) == 1, label
        token_restore_start = _required_index(
            bash_fences[0].body,
            'AWF_PERSISTED_API_TOKEN=""',
            label,
        )
        token_restore_script = (
            bash_fences[0]
            .body[token_restore_start:]
            .split(
                'AWF_PERSISTED_POSTGRES_PASSWORD=""',
                maxsplit=1,
            )[0]
        )
        env_file = tmp_path / ".env"
        env_file.write_text("AWF_API_TOKEN=\n", encoding="utf-8")
        script = "\n".join(
            (
                "unset AWF_API_TOKEN",
                token_restore_script,
                'printf "%s\\n" "$AWF_API_TOKEN"',
            )
        )

        result = subprocess.run(  # noqa: S602
            ["bash", "-c", script],
            cwd=tmp_path,
            check=False,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"{label}: {result.stderr}"
        assert result.stdout == "local-dev-token\n", label


def test_uninstall_source_checkout_restore_accepts_default_api_token(
    tmp_path: Path,
) -> None:
    """Assert source uninstall snippets keep the local default token."""
    uninstall_text = (REPO_ROOT / "docs" / "UNINSTALL.md").read_text(encoding="utf-8")
    cases = (
        (
            "Intro source-checkout uninstall",
            uninstall_text.split("## uv tool", maxsplit=1)[0],
        ),
        (
            "Global source-checkout uninstall",
            _markdown_section(uninstall_text, "## Source Checkout With Global Tool Install"),
        ),
        (
            "No-global source-checkout uninstall",
            _markdown_section(uninstall_text, "## Source Checkout With No Global Install"),
        ),
    )

    for label, section in cases:
        bash_fences = [
            fence
            for fence in _markdown_fences("docs/UNINSTALL.md", section)
            if fence.language == "bash" and 'AWF_PERSISTED_API_TOKEN=""' in fence.body
        ]
        assert len(bash_fences) == 1, label
        token_restore_start = _required_index(
            bash_fences[0].body,
            'AWF_PERSISTED_API_TOKEN=""',
            label,
        )
        token_restore_script = (
            bash_fences[0]
            .body[token_restore_start:]
            .split(
                'AWF_PERSISTED_POSTGRES_PASSWORD=""',
                maxsplit=1,
            )[0]
        )
        env_file = tmp_path / ".env"
        env_file.write_text("AWF_API_TOKEN=\n", encoding="utf-8")
        script = "\n".join(
            (
                "unset AWF_API_TOKEN",
                token_restore_script,
                'printf "%s\\n" "$AWF_API_TOKEN"',
            )
        )

        result = subprocess.run(  # noqa: S602
            ["bash", "-c", script],
            cwd=tmp_path,
            check=False,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"{label}: {result.stderr}"
        assert result.stdout == "local-dev-token\n", label


def test_package_upgrade_env_restore_detects_only_closing_fi_keyword() -> None:
    """Assert lowercase fi in unrelated text is not treated as a shell keyword."""
    upgrade_line = "pipx upgrade agent-workspace-fabric"
    section = (
        "\n"
        + "\n".join(
            [
                upgrade_line,
                *DOTENV_DOUBLE_QUOTE_DECODE_FUNCTION_LINES,
                *DOTENV_UNQUOTED_INLINE_COMMENT_STRIP_FUNCTION_LINES,
                PACKAGE_ENV_READ_LINES["AWF_API_TOKEN"],
                PACKAGE_ENV_INLINE_COMMENT_STRIP_LINES["AWF_API_TOKEN"],
                *PACKAGE_ENV_QUOTE_STRIP_LINES["AWF_API_TOKEN"],
                'if [ -n "$AWF_PERSISTED_API_TOKEN" ]; then',
                '  export AWF_API_TOKEN="$AWF_PERSISTED_API_TOKEN"',
                "else",
                (
                    '  : "${AWF_API_TOKEN:?restore the AWF_API_TOKEN used for the running '
                    'local Core or persist it in .env before upgrading}"'
                ),
                "  export AWF_API_TOKEN",
                "  # awf_config_file can be configured elsewhere",
                PACKAGE_ENV_READ_LINES["AWF_POSTGRES_PASSWORD"],
                PACKAGE_ENV_INLINE_COMMENT_STRIP_LINES["AWF_POSTGRES_PASSWORD"],
                *PACKAGE_ENV_QUOTE_STRIP_LINES["AWF_POSTGRES_PASSWORD"],
                'if [ -n "$AWF_PERSISTED_POSTGRES_PASSWORD" ]; then',
                '  export AWF_POSTGRES_PASSWORD="$AWF_PERSISTED_POSTGRES_PASSWORD"',
                "else",
                (
                    '  : "${AWF_POSTGRES_PASSWORD:?restore the AWF_POSTGRES_PASSWORD used '
                    'for the running local Core or persist it in .env before upgrading}"'
                ),
                "  export AWF_POSTGRES_PASSWORD",
                "  # awf_config_file fallback stays outside persisted .env",
                *_package_database_url_restore_lines(),
                "fi",
                "fi",
                "awf start",
            ]
        )
        + "\n"
    )

    with pytest.raises(AssertionError, match="missing shell line"):
        _assert_package_upgrade_restores_service_env("example", section, upgrade_line)


def test_package_upgrade_env_restore_matches_restart_command_line() -> None:
    """Assert prose mentions of awf start do not satisfy restart command checks."""
    upgrade_line = "pipx upgrade agent-workspace-fabric"
    section = "\n".join(
        [
            upgrade_line,
            *DOTENV_DOUBLE_QUOTE_DECODE_FUNCTION_LINES,
            *DOTENV_UNQUOTED_INLINE_COMMENT_STRIP_FUNCTION_LINES,
            PACKAGE_ENV_READ_LINES["AWF_API_TOKEN"],
            PACKAGE_ENV_INLINE_COMMENT_STRIP_LINES["AWF_API_TOKEN"],
            *PACKAGE_ENV_QUOTE_STRIP_LINES["AWF_API_TOKEN"],
            'if [ -n "$AWF_PERSISTED_API_TOKEN" ]; then',
            '  export AWF_API_TOKEN="$AWF_PERSISTED_API_TOKEN"',
            "else",
            (
                '  : "${AWF_API_TOKEN:?restore the AWF_API_TOKEN used for the running '
                'local Core or persist it in .env before upgrading}"'
            ),
            "  export AWF_API_TOKEN",
            "fi",
            PACKAGE_ENV_READ_LINES["AWF_POSTGRES_PASSWORD"],
            PACKAGE_ENV_INLINE_COMMENT_STRIP_LINES["AWF_POSTGRES_PASSWORD"],
            *PACKAGE_ENV_QUOTE_STRIP_LINES["AWF_POSTGRES_PASSWORD"],
            'if [ -n "$AWF_PERSISTED_POSTGRES_PASSWORD" ]; then',
            '  export AWF_POSTGRES_PASSWORD="$AWF_PERSISTED_POSTGRES_PASSWORD"',
            "else",
            (
                '  : "${AWF_POSTGRES_PASSWORD:?restore the AWF_POSTGRES_PASSWORD used '
                'for the running local Core or persist it in .env before upgrading}"'
            ),
            "  export AWF_POSTGRES_PASSWORD",
            "fi",
            *_package_database_url_restore_lines(),
            "Before running awf start, inspect the saved environment.",
        ]
    )

    with pytest.raises(AssertionError, match="missing restart command"):
        _assert_package_upgrade_restores_service_env("example", section, upgrade_line)


def test_package_upgrade_env_restore_rejects_prefixed_api_export_line() -> None:
    """Assert prefixed AWF_API_TOKEN export lines do not satisfy shell guards."""
    upgrade_line = "pipx upgrade agent-workspace-fabric"
    section = (
        "\n".join(
            [
                upgrade_line,
                *DOTENV_DOUBLE_QUOTE_DECODE_FUNCTION_LINES,
                *DOTENV_UNQUOTED_INLINE_COMMENT_STRIP_FUNCTION_LINES,
                PACKAGE_ENV_READ_LINES["AWF_API_TOKEN"],
                PACKAGE_ENV_INLINE_COMMENT_STRIP_LINES["AWF_API_TOKEN"],
                *PACKAGE_ENV_QUOTE_STRIP_LINES["AWF_API_TOKEN"],
                'if [ -n "$AWF_PERSISTED_API_TOKEN" ]; then',
                "  export AWF_API_TOKEN_BACKUP",
                "else",
                (
                    '  : "${AWF_API_TOKEN:?restore the AWF_API_TOKEN used for the running '
                    'local Core or persist it in .env before upgrading}"'
                ),
                "  export AWF_API_TOKEN",
                "fi",
                PACKAGE_ENV_READ_LINES["AWF_POSTGRES_PASSWORD"],
                PACKAGE_ENV_INLINE_COMMENT_STRIP_LINES["AWF_POSTGRES_PASSWORD"],
                *PACKAGE_ENV_QUOTE_STRIP_LINES["AWF_POSTGRES_PASSWORD"],
                'if [ -n "$AWF_PERSISTED_POSTGRES_PASSWORD" ]; then',
                '  export AWF_POSTGRES_PASSWORD="$AWF_PERSISTED_POSTGRES_PASSWORD"',
                "else",
                (
                    '  : "${AWF_POSTGRES_PASSWORD:?restore the AWF_POSTGRES_PASSWORD used '
                    'for the running local Core or persist it in .env before upgrading}"'
                ),
                "  export AWF_POSTGRES_PASSWORD",
                "fi",
                "awf start",
            ]
        )
        + "\n"
    )

    with pytest.raises(AssertionError, match="must export persisted AWF_API_TOKEN"):
        _assert_package_upgrade_restores_service_env("example", section, upgrade_line)


def test_upgrade_release_installed_rollback_restores_service_env_before_start() -> None:
    """Assert release-installed rollback keeps mandatory service env available."""
    upgrade_text = (REPO_ROOT / "docs" / "UPGRADE.md").read_text(encoding="utf-8")
    rollback_section = _markdown_section(upgrade_text, "## Rollback")
    release_heading = "For release-installed lanes"
    source_heading = "For the source checkout with global tool install lane"

    release_section = _required_rollback_subsection(
        rollback_section,
        release_heading,
        end_marker=source_heading,
    )
    _assert_package_upgrade_restores_service_env(
        "release-installed rollback",
        release_section,
        upgrade_line=None,
        lifecycle="rollback",
    )


def test_quickstart_source_checkout_upgrades_reuse_existing_checkout() -> None:
    """Assert source-checkout upgrade commands reuse the checkout created earlier."""
    quickstart_text = (REPO_ROOT / "docs" / "QUICKSTART.md").read_text(encoding="utf-8")
    source_lane_headings = (
        "## Lane 2: Source Checkout With Global Tool Install",
        "## Lane 3: Source Checkout With No Global Install",
    )

    for heading in source_lane_headings:
        upgrade_section = _quickstart_upgrade_section(quickstart_text, heading)

        assert "from the existing `aira-agent-workspace-fabric` checkout" in upgrade_section
        assert "`cd /path/to/aira-agent-workspace-fabric`" in upgrade_section
        assert "cd aira-agent-workspace-fabric" not in upgrade_section
        assert "git pull" in upgrade_section


def test_quickstart_first_run_urls_use_ipv4_loopback() -> None:
    """Assert Quickstart local service URLs use IPv4 loopback copy-paste URLs."""
    quickstart_text = (REPO_ROOT / "docs" / "QUICKSTART.md").read_text(encoding="utf-8")

    assert "`http://127.0.0.1:8000` for API checks" in quickstart_text
    assert "`http://127.0.0.1:3000` when the console is running" in quickstart_text
    assert "`http://127.0.0.1:3000` for the console" in quickstart_text
    assert "`http://127.0.0.1:8000/readyz`" in quickstart_text
    assert not re.search(r"http://localhost:(?:3000|8000)\b", quickstart_text)


def test_getting_started_first_run_urls_match_smoke_defaults() -> None:
    """Assert Getting Started local URL prose matches smoke probe defaults."""
    getting_started_text = (REPO_ROOT / "docs" / "GETTING_STARTED.md").read_text(
        encoding="utf-8",
    )
    startup_heading = "### Recommended First-Run Sequence"
    configure_heading = "### Configure Environment"
    startup_section = _markdown_section_between(
        getting_started_text,
        startup_heading,
        configure_heading,
    )

    assert not re.search(
        r"using\s+`127\.0\.0\.1`\s+for host-facing loopback",
        startup_section,
    )
    assert re.search(r"current smoke\s+defaults", startup_section)


def test_getting_started_manual_local_api_urls_use_localhost() -> None:
    """Assert manual host-side URL override examples keep the public docs style."""
    getting_started_text = (REPO_ROOT / "docs" / "GETTING_STARTED.md").read_text(
        encoding="utf-8",
    )
    manual_url_section = getting_started_text.split(
        "For host-side `awf` workspace commands and manual HTTP checks",
        maxsplit=1,
    )[1].split("`AWF_API_BASE_URL` is different", maxsplit=1)[0]

    assert not re.search(r"http://127\.0\.0\.1:(?:3000|8000)\b", manual_url_section)
    assert "http://localhost:<port>" in manual_url_section
    assert "http://localhost:${AWF_API_HOST_PORT}" in manual_url_section


def test_markdown_section_accepts_trailing_heading_whitespace() -> None:
    """Assert section extraction tolerates harmless heading whitespace."""
    text = "Intro\n## Target \t\nbody\n## Next\nother\n"

    assert _markdown_section(text, "## Target") == "body\n"


def test_markdown_section_reports_missing_heading_clearly() -> None:
    """Assert missing section headings fail with a useful assertion message."""
    with pytest.raises(AssertionError, match=r"Markdown heading '## Missing' not found"):
        _markdown_section("## Present\nbody\n", "## Missing")


def test_markdown_section_between_reports_missing_start_heading_clearly() -> None:
    """Assert missing start headings fail with a useful assertion message."""
    with pytest.raises(AssertionError, match=r"Markdown heading '### Missing' not found"):
        _markdown_section_between(
            "### Present\nbody\n### Next\nother\n",
            "### Missing",
            "### Next",
        )


def test_markdown_section_between_reports_missing_end_heading_clearly() -> None:
    """Assert missing end headings identify the preceding heading."""
    with pytest.raises(
        AssertionError,
        match=(
            r"Markdown heading '### Missing' not found after "
            r"'### Recommended First-Run Sequence'"
        ),
    ):
        _markdown_section_between(
            "### Recommended First-Run Sequence\nbody\n",
            "### Recommended First-Run Sequence",
            "### Missing",
        )


@pytest.mark.parametrize("heading", ("### Target", "#### Target"))
def test_markdown_section_rejects_h3_or_deeper_headings(heading: str) -> None:
    """Assert unsupported heading depth fails instead of over-capturing."""
    text = "## Parent\nintro\n### Target\nbody\n### Next\nother\n"

    with pytest.raises(ValueError, match=r"Only H2 headings are supported"):
        _markdown_section(text, heading)


def test_required_index_reports_missing_text_after_start_clearly() -> None:
    """Assert ordered doc checks report assertion failures, not ValueError."""
    text = "if [ -f docker/compose/.env ]; then\nfi\nfallback\n"
    fallback_index = text.find("fallback")
    assert fallback_index != -1, "test fixture is missing fallback marker"

    with pytest.raises(AssertionError, match="example is missing required text after offset"):
        _required_index(text, "\nfi\n", "example", start=fallback_index)


def test_quickstart_upgrade_section_requires_uninstall_after_upgrade() -> None:
    """Assert malformed lane labels fail with a targeted assertion."""
    text = "## Lane\nUninstall:\nremove\nUpgrade:\nupgrade\n## Next\n"

    with pytest.raises(AssertionError, match="## Lane is missing Uninstall block after Upgrade"):
        _quickstart_upgrade_section(text, "## Lane")


def test_quickstart_uses_runnable_startup_path() -> None:
    """Assert Quickstart lanes use setup/start/status before project init."""
    quickstart_text = (REPO_ROOT / "docs" / "QUICKSTART.md").read_text(encoding="utf-8")

    assert "All lanes use root `.env` for local runtime values" in quickstart_text
    assert "AWF_SETUP_PLACEHOLDER" not in quickstart_text
    assert "AWF_START_PLACEHOLDER" not in quickstart_text

    for heading in (
        "## Lane 1: uv tool or pipx",
        "## Lane 2: Source Checkout With Global Tool Install",
        "## Lane 3: Source Checkout With No Global Install",
    ):
        first_run_section = _markdown_section(quickstart_text, heading).split(
            "\nUpgrade:\n",
            maxsplit=1,
        )[0]
        assert "awf setup" in first_run_section
        assert "awf start" in first_run_section
        assert "awf service status --format pretty" in first_run_section
        assert "awf init" in first_run_section
        assert "awf smoke run" in first_run_section

    assert "docker/compose/.env" in quickstart_text
    assert "migration sources only" in quickstart_text


def test_quickstart_token_refresh_restart_is_lane_aware() -> None:
    """Assert shared token-refresh guidance has runnable lane restarts."""
    quickstart_text = (REPO_ROOT / "docs" / "QUICKSTART.md").read_text(encoding="utf-8")
    prerequisites_section = _markdown_section(quickstart_text, "## Prerequisites")
    restart_section = prerequisites_section.split(
        "If you set or refresh the GitHub token",
        maxsplit=1,
    )[1]

    assert "For Lane 1" in restart_section
    assert "For Lane 2" in restart_section
    assert "For Lane 3" in restart_section
    assert re.search(r"(?m)^awf start\s*$", restart_section)
    assert re.search(r'(?m)^awf start --source-checkout "\$PWD"\s*$', restart_section)
    assert re.search(
        (
            r"(?m)^uv run --python 3\.12 --extra dev awf start "
            r'--source-checkout "\$PWD"\s*$'
        ),
        restart_section,
    )
    assert not re.search(r"(?m)^uv run --python 3\.12 --extra dev awf start\s*$", restart_section)


def test_raw_docker_compose_source_path_is_single_command() -> None:
    """Assert raw Docker guidance remains a single runnable command."""
    quickstart_text = (REPO_ROOT / "docs" / "QUICKSTART.md").read_text(encoding="utf-8")
    getting_started_text = (REPO_ROOT / "docs" / "GETTING_STARTED.md").read_text(encoding="utf-8")

    for doc_name, text in (
        ("QUICKSTART.md", quickstart_text),
        ("GETTING_STARTED.md", getting_started_text),
    ):
        section = text.split(
            "For source checkouts or raw Docker installs",
            maxsplit=1,
        )[1].split("If ", maxsplit=1)[0]
        assert "docker compose up --build" in section, doc_name
        assert "cp .env.example .env" not in section, doc_name
        assert "docker build -t awf-agent-runtime:latest" not in section, doc_name
        assert "127.0.0.1:3000" in section, doc_name
        assert "127.0.0.1:8000" in section, doc_name
        assert "localhost:3000" not in section, doc_name
        assert "localhost:8000" not in section, doc_name
        assert "local-dev-token" in section, doc_name


def test_getting_started_uses_runnable_startup_path() -> None:
    """Assert Getting Started uses setup/start before project initialization."""
    getting_started_text = (REPO_ROOT / "docs" / "GETTING_STARTED.md").read_text(encoding="utf-8")
    startup_section = _markdown_section_between(
        getting_started_text,
        "### Recommended First-Run Sequence",
        "### Configure Environment",
    )
    configure_section = _markdown_section_between(
        getting_started_text,
        "### Configure Environment",
        "### Local vs Production Configuration",
    )

    assert len(re.findall(r"(?m)^awf setup\s*$", startup_section)) == 1
    assert len(re.findall(r"(?m)^awf start\s*$", startup_section)) == 1
    assert len(re.findall(r'(?m)^awf setup --source-checkout "\$PWD"\s*$', startup_section)) == 1
    assert len(re.findall(r'(?m)^awf start --source-checkout "\$PWD"\s*$', startup_section)) == 1
    assert (
        len(
            re.findall(
                (
                    r"(?m)^uv run --python 3\.12 --extra dev awf setup "
                    r'--source-checkout "\$PWD"\s*$'
                ),
                startup_section,
            )
        )
        == 1
    )
    assert (
        len(
            re.findall(
                (
                    r"(?m)^uv run --python 3\.12 --extra dev awf start "
                    r'--source-checkout "\$PWD"\s*$'
                ),
                startup_section,
            )
        )
        == 1
    )
    assert not re.search(
        r"(?m)^uv run --python 3\.12 --extra dev awf (setup|start)\s*$",
        startup_section,
    )
    assert "awf init <path> --write-profile --yes" in startup_section
    assert "awf smoke run --project <path> --mocked-local --format pretty" in startup_section
    assert "AWF_SETUP_PLACEHOLDER" not in startup_section
    assert "AWF_START_PLACEHOLDER" not in startup_section
    assert not re.search(r"(?m)^awf service bootstrap\s*$", startup_section)
    assert "Root `.env` is the single local runtime env file" in configure_section
    assert "`awf start`, `awf service bootstrap`, and raw root `docker compose`" in (
        configure_section
    )
    assert "migration source" in configure_section
