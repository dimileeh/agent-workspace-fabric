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
    _package_env_restore_script,
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
        assert checkout_refresh_line in section, f"{label} is missing checkout refresh"
        assert refresh_prereq in section, f"{label} is missing upgrade prerequisite"
        (
            env_restore_start_index,
            env_restore_end_index,
            _stop_start_index,
            stop_end_index,
        ) = _assert_source_checkout_service_env_restore_and_stop(
            label,
            section,
            "upgrading",
            require_legacy_fallback=not label.startswith("Quickstart"),
        )
        assert setup_line in section, f"{label} does not refresh source_checkout metadata"
        assert start_line in section, f"{label} is missing source-checkout start"
        checkout_refresh_index = section.index(checkout_refresh_line)
        assert (
            env_restore_start_index
            < env_restore_end_index
            < stop_end_index
            < checkout_refresh_index
            < section.index(refresh_prereq)
            < section.index(setup_line)
            < section.index(start_line)
        ), f"{label} must stop Core before refreshing source files"


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
    release_rollback_section = rollback_section.split(
        "For release-installed lanes",
        maxsplit=1,
    )[1].split(
        "For the source checkout with global tool install lane",
        maxsplit=1,
    )[0]
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
    release_rollback_section = rollback_section.split(
        "For release-installed lanes",
        maxsplit=1,
    )[1].split(
        global_rollback_heading,
        maxsplit=1,
    )[0]
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
            rollback_section.split(global_rollback_heading, maxsplit=1)[1].split(
                no_global_rollback_heading,
                maxsplit=1,
            )[0],
        ),
        (
            "No-global source-checkout rollback",
            rollback_section.split(no_global_rollback_heading, maxsplit=1)[1],
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
        restore_start = body.index(DOTENV_DOUBLE_QUOTE_DECODE_FUNCTION_LINES[0])
        stop_start = body.index("if [ -f .env ]; then")
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
                f'{persisted_var}="$(sed -n {shlex.quote(expression)} "$env_file" | head -n 1)"',
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
            rollback_section.split(global_rollback_heading, maxsplit=1)[1].split(
                no_global_rollback_heading,
                maxsplit=1,
            )[0],
        ),
        (
            "No-global source-checkout rollback",
            rollback_section.split(no_global_rollback_heading, maxsplit=1)[1],
        ),
    )

    for label, section in cases:
        bash_fences = [
            fence
            for fence in _markdown_fences("docs/UPGRADE.md", section)
            if fence.language == "bash"
        ]
        assert len(bash_fences) == 1, label
        token_restore_start = bash_fences[0].body.index('AWF_PERSISTED_API_TOKEN=""')
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
        token_restore_start = bash_fences[0].body.index('AWF_PERSISTED_API_TOKEN=""')
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

    assert release_heading in rollback_section
    assert source_heading in rollback_section
    release_section = rollback_section.split(release_heading, maxsplit=1)[1].split(
        source_heading,
        maxsplit=1,
    )[0]
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
    assert startup_heading in getting_started_text
    assert configure_heading in getting_started_text
    startup_section = getting_started_text.split(startup_heading, maxsplit=1)[1].split(
        configure_heading,
        maxsplit=1,
    )[0]

    assert not re.search(
        r"using\s+`127\.0\.0\.1`\s+for host-facing loopback",
        startup_section,
    )
    assert re.search(r"current smoke\s+defaults", startup_section)


def test_getting_started_direct_local_api_urls_use_localhost() -> None:
    """Assert host-facing local API/console URLs use the public docs style."""
    getting_started_text = (REPO_ROOT / "docs" / "GETTING_STARTED.md").read_text(
        encoding="utf-8",
    )

    assert not re.search(r"http://127\.0\.0\.1:(?:3000|8000)\b", getting_started_text)
    assert "http://localhost:8000" in getting_started_text
    assert "http://localhost:3000" in getting_started_text


def test_markdown_section_accepts_trailing_heading_whitespace() -> None:
    """Assert section extraction tolerates harmless heading whitespace."""
    text = "Intro\n## Target \t\nbody\n## Next\nother\n"

    assert _markdown_section(text, "## Target") == "body\n"


def test_markdown_section_reports_missing_heading_clearly() -> None:
    """Assert missing section headings fail with a useful assertion message."""
    with pytest.raises(AssertionError, match=r"Markdown heading '## Missing' not found"):
        _markdown_section("## Present\nbody\n", "## Missing")


@pytest.mark.parametrize("heading", ("### Target", "#### Target"))
def test_markdown_section_rejects_h3_or_deeper_headings(heading: str) -> None:
    """Assert unsupported heading depth fails instead of over-capturing."""
    text = "## Parent\nintro\n### Target\nbody\n### Next\nother\n"

    with pytest.raises(ValueError, match=r"Only H2 headings are supported"):
        _markdown_section(text, heading)


def test_required_index_reports_missing_text_after_start_clearly() -> None:
    """Assert ordered doc checks report assertion failures, not ValueError."""
    text = "if [ -f docker/compose/.env ]; then\nfi\nfallback\n"

    with pytest.raises(AssertionError, match="example is missing required text after offset"):
        _required_index(text, "\nfi\n", "example", start=text.index("fallback"))


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
        if doc_name == "QUICKSTART.md":
            assert "127.0.0.1:3000" in section, doc_name
            assert "127.0.0.1:8000" in section, doc_name
            assert "localhost:3000" not in section, doc_name
            assert "localhost:8000" not in section, doc_name
        else:
            assert "localhost:3000" in section, doc_name
            assert "localhost:8000" in section, doc_name
        assert "local-dev-token" in section, doc_name


def test_getting_started_uses_runnable_startup_path() -> None:
    """Assert Getting Started uses setup/start before project initialization."""
    getting_started_text = (REPO_ROOT / "docs" / "GETTING_STARTED.md").read_text(encoding="utf-8")
    startup_section = getting_started_text.split(
        "### Recommended First-Run Sequence",
        maxsplit=1,
    )[1].split("### Configure Environment", maxsplit=1)[0]
    configure_section = getting_started_text.split(
        "### Configure Environment",
        maxsplit=1,
    )[1].split("### Local vs Production Configuration", maxsplit=1)[0]

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
