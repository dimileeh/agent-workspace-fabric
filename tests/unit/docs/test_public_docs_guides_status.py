from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path
from urllib.parse import quote

import pytest

from tests.unit.docs import public_docs_status_helpers as helpers
from tests.unit.docs.public_docs_status_helpers import (
    PACKAGE_DATABASE_URL_ENCODED_EXPORT,
    PACKAGE_DATABASE_URL_RAW_PASSWORD_EXPORT,
    PACKAGE_POSTGRES_PASSWORD_URLENCODE_LINE,
    README_PATH,
    REPO_ROOT,
    MarkdownFence,
    _assert_snippet_syntax,
    _assert_source_checkout_service_env_restore_and_stop,
    _awf_command_mentions,
    _copy_paste_docs,
    _fence_delimiter_count_is_even,
    _markdown_fences,
    _markdown_section,
    _markdown_section_between,
    _public_docs,
    _required_index,
)


def test_getting_started_first_run_persists_service_env_for_upgrade() -> None:
    """Assert Getting Started first-run commands create root `.env` before startup."""
    getting_started_text = (REPO_ROOT / "docs" / "GETTING_STARTED.md").read_text(
        encoding="utf-8",
    )
    startup_section = _markdown_section_between(
        getting_started_text,
        "### Recommended First-Run Sequence",
        "### Configure Environment",
    )
    package_heading = "For package-manager or virtualenv installs:"
    source_global_heading = (
        "For a source checkout with a global `awf` executable, run from the checkout:"
    )
    source_no_global_heading = (
        "For a source checkout with no global install, run from the checkout:"
    )

    assert package_heading in startup_section
    assert source_global_heading in startup_section
    assert source_no_global_heading in startup_section
    package_section = startup_section.split(package_heading, maxsplit=1)[1].split(
        source_global_heading,
        maxsplit=1,
    )[0]
    source_cases = (
        (
            "source checkout with global executable",
            startup_section.split(source_global_heading, maxsplit=1)[1].split(
                source_no_global_heading,
                maxsplit=1,
            )[0],
            '\nawf setup --source-checkout "$PWD"\n',
        ),
        (
            "source checkout with no global install",
            startup_section.split(source_no_global_heading, maxsplit=1)[1].split(
                "\n\nThe `setup` command",
                maxsplit=1,
            )[0],
            '\nuv run --python 3.12 --extra dev awf setup --source-checkout "$PWD"\n',
        ),
    )
    package_api_export = 'export AWF_API_TOKEN="$(openssl rand -hex 32)"'
    package_password_export = 'export AWF_POSTGRES_PASSWORD="${AWF_POSTGRES_PASSWORD:-awf_dev}"'
    package_host_port_export = 'export AWF_POSTGRES_HOST_PORT="${AWF_POSTGRES_HOST_PORT:-5433}"'
    package_password_dotenv_export = 'awf_postgres_password_dotenv="$('
    package_env_tmp = 'awf_env_tmp="$(mktemp)"'
    package_password_persist = (
        "  printf 'AWF_POSTGRES_PASSWORD=%s\\n' \"$awf_postgres_password_dotenv\""
    )
    package_persist_target = 'mv "$awf_env_tmp" .env'
    package_setup_command = "\nawf setup\n"

    assert "cp .env.example .env" not in package_section
    assert package_api_export in package_section
    assert package_password_export in package_section
    assert package_host_port_export in package_section
    assert PACKAGE_POSTGRES_PASSWORD_URLENCODE_LINE in package_section
    assert package_password_dotenv_export in package_section
    assert "AWF_POSTGRES_PASSWORD cannot contain newlines" in package_section
    assert PACKAGE_DATABASE_URL_ENCODED_EXPORT in package_section
    assert PACKAGE_DATABASE_URL_RAW_PASSWORD_EXPORT not in package_section
    assert package_env_tmp in package_section
    assert "  printf 'AWF_API_TOKEN=%s\\n' \"$AWF_API_TOKEN\"" in package_section
    assert package_password_persist in package_section
    assert "  printf 'AWF_POSTGRES_PASSWORD=%s\\n' \"$AWF_POSTGRES_PASSWORD\"" not in (
        package_section
    )
    assert "  printf 'AWF_POSTGRES_HOST_PORT=%s\\n' \"$AWF_POSTGRES_HOST_PORT\"" in package_section
    assert "  printf 'AWF_DATABASE_URL=%s\\n' \"$AWF_DATABASE_URL\"" in package_section
    assert package_persist_target in package_section
    assert package_section.index(package_api_export) < package_section.index(package_env_tmp)
    assert package_section.index(package_password_export) < package_section.index(
        PACKAGE_POSTGRES_PASSWORD_URLENCODE_LINE,
    )
    assert package_section.index(PACKAGE_POSTGRES_PASSWORD_URLENCODE_LINE) < (
        package_section.index(package_password_dotenv_export)
    )
    assert package_section.index(package_password_dotenv_export) < (
        package_section.index(PACKAGE_DATABASE_URL_ENCODED_EXPORT)
    )
    assert package_section.index(package_env_tmp) < package_section.index(package_persist_target)
    assert _required_index(package_section, package_persist_target, "package first-run") < (
        _required_index(package_section, package_setup_command, "package first-run")
    )

    for label, section, setup_command in source_cases:
        assert "cp .env.example .env" in section, f"{label} must create root .env"
        assert "awf_env_tmp" not in section, f"{label} should not use legacy env rewrite"
        assert "docker/compose/.env" not in section, f"{label} should not write compose env"
        assert _required_index(section, "cp .env.example .env", label) < (
            _required_index(section, setup_command, label)
        ), f"{label} must create root .env before setup"


def test_getting_started_package_first_run_url_encodes_custom_postgres_password(
    tmp_path: Path,
) -> None:
    """Assert Getting Started package first-run env matches Quickstart escaping."""
    from awf.service.environment import compose_env_file_values

    getting_started_text = (REPO_ROOT / "docs" / "GETTING_STARTED.md").read_text(
        encoding="utf-8",
    )
    startup_section = _markdown_section_between(
        getting_started_text,
        "### Recommended First-Run Sequence",
        "### Configure Environment",
    )
    package_section = startup_section.split(
        "For package-manager or virtualenv installs:",
        maxsplit=1,
    )[1].split(
        "For a source checkout with a global `awf` executable, run from the checkout:",
        maxsplit=1,
    )[0]
    bash_fences = [
        fence
        for fence in _markdown_fences("docs/GETTING_STARTED.md", package_section)
        if fence.language == "bash" and "AWF_DATABASE_URL" in fence.body
    ]
    assert len(bash_fences) == 1
    env_persist_script = bash_fences[0].body.split("\nawf setup\n", maxsplit=1)[0]
    custom_password = 'p@ss/word:with $dollar #hash "quote" \\path'
    expected_url = f"postgresql+asyncpg://awf:{quote(custom_password, safe='')}@localhost:5433/awf"
    expected_dotenv_password = (
        '"' + custom_password.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$") + '"'
    )
    script = "\n".join(
        (
            f"export AWF_POSTGRES_PASSWORD={shlex.quote(custom_password)}",
            env_persist_script,
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
    env_file = tmp_path / ".env"
    env_text = env_file.read_text(encoding="utf-8")
    assert f"AWF_POSTGRES_PASSWORD={expected_dotenv_password}\n" in env_text
    assert f"AWF_DATABASE_URL={expected_url}\n" in env_text
    assert compose_env_file_values(env_file, environ={})["AWF_POSTGRES_PASSWORD"] == custom_password
    assert custom_password not in env_text.split("AWF_DATABASE_URL=", maxsplit=1)[1]


def test_getting_started_package_first_run_uses_generated_root_env() -> None:
    """Assert Getting Started package first run does not copy source-only assets."""
    getting_started_text = (REPO_ROOT / "docs" / "GETTING_STARTED.md").read_text(
        encoding="utf-8",
    )
    startup_section = _markdown_section_between(
        getting_started_text,
        "### Recommended First-Run Sequence",
        "### Configure Environment",
    )
    package_section = startup_section.split(
        "For package-manager or virtualenv installs:",
        maxsplit=1,
    )[1].split(
        "For a source checkout with a global `awf` executable, run from the checkout:",
        maxsplit=1,
    )[0]

    assert "cp .env.example .env" not in package_section
    assert 'awf_env_tmp="$(mktemp)"' in package_section
    assert 'awf_postgres_password_dotenv="$(' in package_section
    assert "if [ -f .env ]; then" in package_section
    assert "AWF_API_TOKEN[[:space:]]*=/d" in package_section
    assert "AWF_POSTGRES_PASSWORD[[:space:]]*=/d" in package_section
    assert "AWF_POSTGRES_HOST_PORT[[:space:]]*=/d" in package_section
    assert "AWF_DATABASE_URL[[:space:]]*=/d" in package_section
    assert "docker/compose/.env" not in package_section
    assert 'mv "$awf_env_tmp" .env' in package_section
    assert package_section.index('mv "$awf_env_tmp" .env') < package_section.index(
        "\nawf setup\n",
    )


def test_getting_started_mocked_smoke_keeps_github_auth_optional() -> None:
    """Assert Getting Started first-run smoke does not require GitHub CLI auth."""
    getting_started_text = (REPO_ROOT / "docs" / "GETTING_STARTED.md").read_text(encoding="utf-8")
    startup_section = _markdown_section_between(
        getting_started_text,
        "### Recommended First-Run Sequence",
        "### Configure Environment",
    )

    assert re.search(r"without requiring live GitHub\s+or provider credentials", startup_section)
    assert (
        "# [optional] Only needed for PR creation/monitoring; skip for mocked smoke."
        in startup_section
    )
    assert (
        "# Provide AWF_GITHUB_TOKEN, GH_TOKEN, or GITHUB_TOKEN manually if needed."
        in startup_section
    )
    assert not re.search(
        r'(?m)^export AWF_GITHUB_TOKEN="\$\(gh auth token\)"$',
        startup_section,
    )


def test_getting_started_configure_environment_uses_root_env() -> None:
    """Assert source-checkout configuration writes root `.env`."""
    getting_started_text = (REPO_ROOT / "docs" / "GETTING_STARTED.md").read_text(encoding="utf-8")
    configure_section = _markdown_section_between(
        getting_started_text,
        "### Configure Environment",
        "### Local vs Production Configuration",
    )
    root_env_snippet_start = _required_index(
        configure_section,
        "grep -vE '^(AWF_API_TOKEN|AWF_GITHUB_TOKEN)=' .env.example",
        "Configure Environment",
    )
    root_env_snippet_end = _required_index(
        configure_section,
        "uv run --python 3.12 --extra dev awf service bootstrap",
        "Configure Environment",
    )
    root_env_snippet = configure_section[root_env_snippet_start:root_env_snippet_end]

    assert "Root `.env` is the single local runtime env file" in configure_section
    assert "migration source" in configure_section
    assert "} > .env" in root_env_snippet
    assert "} > docker/compose/.env" not in root_env_snippet
    assert "awf_env_tmp" not in root_env_snippet


def test_getting_started_cli_host_port_derivation_matches_cli_default() -> None:
    """Assert Getting Started documents the CLI's localhost host-port derivation."""
    getting_started_text = (REPO_ROOT / "docs" / "GETTING_STARTED.md").read_text(encoding="utf-8")
    configure_section = _markdown_section_between(
        getting_started_text,
        "### Configure Environment",
        "### Local vs Production Configuration",
    )

    assert (
        "`AWF_API_HOST_PORT` is present, the CLI derives `http://localhost:<port>`"
        in configure_section
    )
    assert 'export AWF_BASE_URL="http://localhost:${AWF_API_HOST_PORT}"' in configure_section
    assert (
        "`AWF_API_HOST_PORT` is present, the CLI derives `http://127.0.0.1:<port>`"
        not in configure_section
    )
    assert "root `.env` is the single local runtime env file" in configure_section.lower()
    assert "docker/compose/.env" in configure_section
    assert "migration source" in configure_section


def test_mcp_setup_prerequisites_use_runnable_startup_path() -> None:
    """Assert MCP setup prerequisites use the canonical local startup flow."""
    mcp_setup_text = (REPO_ROOT / "docs" / "MCP_SETUP.md").read_text(encoding="utf-8")
    prerequisites_section = mcp_setup_text.split("## Prerequisites", maxsplit=1)[1].split(
        "## Claude Code",
        maxsplit=1,
    )[0]

    assert len(re.findall(r"(?m)^awf setup\s*$", prerequisites_section)) == 2
    assert len(re.findall(r"(?m)^awf start\s*$", prerequisites_section)) == 2
    assert (
        len(re.findall(r"(?m)^awf service status --format pretty\s*$", prerequisites_section)) == 2
    )
    assert len(re.findall(r"(?m)^cp \.env\.example \.env$", prerequisites_section)) == 1
    assert "cat > .env <<'EOF'" in prerequisites_section
    assert "AWF_API_TOKEN=local-dev-token" in prerequisites_section
    assert "AWF_POSTGRES_PASSWORD=awf_dev" in prerequisites_section
    assert "docker/compose/.env" not in prerequisites_section


def test_project_onboarding_first_run_uses_runnable_startup_path() -> None:
    """Assert onboarding starts AWF before initializing the project workspace."""
    onboarding_text = (REPO_ROOT / "docs" / "PROJECT_ONBOARDING.md").read_text(encoding="utf-8")
    first_run_section = onboarding_text.split(
        "## First-run operator command",
        maxsplit=1,
    )[1].split("## One-message prompt", maxsplit=1)[0]

    assert "cp .env.example .env" in first_run_section
    assert re.search(r"(?m)^awf setup\s*$", first_run_section)
    assert re.search(r"(?m)^awf start\s*$", first_run_section)
    assert "awf service status --format pretty" in first_run_section
    assert "awf init ." in first_run_section
    assert not re.search(r"(?m)^awf service bootstrap\s*$", first_run_section)
    assert "AWF_SETUP_PLACEHOLDER" not in first_run_section
    assert "AWF_START_PLACEHOLDER" not in first_run_section


def test_project_onboarding_docs_make_awf_init_primary() -> None:
    """Assert project onboarding documents path-based awf init as the primary flow."""
    quickstart_text = (REPO_ROOT / "docs" / "QUICKSTART.md").read_text(encoding="utf-8")
    getting_started_text = (REPO_ROOT / "docs" / "GETTING_STARTED.md").read_text(encoding="utf-8")
    onboarding_text = (REPO_ROOT / "docs" / "PROJECT_ONBOARDING.md").read_text(encoding="utf-8")

    assert "awf setup" in quickstart_text
    assert "awf start" in quickstart_text
    assert "awf init <path>" in quickstart_text
    assert "awf setup" in getting_started_text
    assert "awf start" in getting_started_text
    assert "awf init <path> --write-profile --yes" in getting_started_text
    assert "awf init . --write-profile --yes" in onboarding_text
    assert "v2 request-shaped" not in onboarding_text
    assert "awf profile init . --write" not in quickstart_text


def test_public_docs_do_not_describe_no_path_init_as_service_bootstrap() -> None:
    """Assert public docs do not revive the old no-path init bootstrap grammar."""
    public_paths = [Path("README.md"), *map(Path, sorted(_public_docs()))]
    forbidden_patterns = [
        r"`awf init`\s+without a path",
        r"without a path,?\s+`awf init`",
        r"`awf init`\s+\(no path\)",
        r"no-path\s+`awf init`",
        r"bare\s+`awf init`",
        r"(?m)^\s*awf init\s*(?:#\s*.*bootstrap.*)?$",
        r"`awf init`\s+or\s+`awf service bootstrap`",
        r"after `awf init` or `awf service bootstrap`",
        r"`awf init`\s+writes the local service environment",
        r"run `awf init` to verify prerequisites and bootstrap",
        r"`awf init`\. With no arguments it bootstraps",
    ]

    offenders: list[str] = []
    public_texts: list[str] = []
    for rel_path in public_paths:
        text = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
        public_texts.append(text)
        for pattern in forbidden_patterns:
            if re.search(pattern, text, flags=re.IGNORECASE):
                offenders.append(f"{rel_path}: {pattern}")

    public_text = "\n".join(public_texts)
    assert not offenders
    assert "awf setup" in public_text
    assert "awf start" in public_text


def test_changelog_upgrade_and_uninstall_guides_are_discoverable() -> None:
    """Assert release lifecycle docs are linked from the public README."""
    readme_text = README_PATH.read_text(encoding="utf-8")
    upgrade_text = (REPO_ROOT / "docs" / "UPGRADE.md").read_text(encoding="utf-8")

    assert (REPO_ROOT / "CHANGELOG.md").exists()
    assert (REPO_ROOT / "docs" / "UPGRADE.md").exists()
    assert (REPO_ROOT / "docs" / "UNINSTALL.md").exists()
    assert (REPO_ROOT / "RELEASING.md").exists()
    assert "[Changelog](CHANGELOG.md)" in readme_text
    assert "[Upgrade Guide](docs/UPGRADE.md)" in readme_text
    assert "[Uninstall Guide](docs/UNINSTALL.md)" in readme_text
    assert "[Release Checklist](RELEASING.md)" in readme_text
    assert "[Local Service Upgrade](CONCEPTS.md#local-service-upgrade)" in upgrade_text
    assert "[Local Service Rollback](CONCEPTS.md#local-service-rollback)" in upgrade_text
    assert "pre-upgrade Postgres backup" in upgrade_text


def test_upgrade_and_uninstall_docs_cover_all_first_run_lanes() -> None:
    """Assert upgrade and uninstall guides cover each currently available lane."""
    upgrade_text = (REPO_ROOT / "docs" / "UPGRADE.md").read_text(encoding="utf-8")
    uninstall_text = (REPO_ROOT / "docs" / "UNINSTALL.md").read_text(encoding="utf-8")

    lane_terms = (
        "uv tool",
        "pipx",
        "source checkout with global tool install",
        "source checkout with no global install",
    )
    for lane_term in lane_terms:
        assert lane_term in upgrade_text.lower()
        assert lane_term in uninstall_text.lower()

    assert "curl -fsSL https://aira.pro/install.sh" not in upgrade_text
    assert "uv tool upgrade agent-workspace-fabric" in upgrade_text
    assert "pipx upgrade agent-workspace-fabric" in upgrade_text
    assert "git pull" in upgrade_text
    assert "awf start" in upgrade_text
    assert "awf smoke run --project <path> --mocked-local --format pretty" in upgrade_text
    assert "uv run --python 3.12 --extra dev awf smoke run --project <path>" in upgrade_text
    upgrade_smoke_lines = [line for line in upgrade_text.splitlines() if "smoke run" in line]
    assert upgrade_smoke_lines
    assert all("--project <path>" in line for line in upgrade_smoke_lines)

    assert "curl -fsSL https://aira.pro/install.sh" not in uninstall_text
    assert "uv tool uninstall agent-workspace-fabric" in uninstall_text
    assert "pipx uninstall agent-workspace-fabric" in uninstall_text
    assert "rm -rf" in uninstall_text
    assert "~/.awf/config.yml" in uninstall_text
    assert uninstall_text.index("~/.awf/config.yml") < uninstall_text.index("rm -rf")
    assert "source_checkout" in uninstall_text
    assert "awf setup --source-checkout /path/to/replacement/aira-agent-workspace-fabric" in (
        uninstall_text
    )
    assert "remove only the top-level `source_checkout:` block" in uninstall_text
    assert "does not delete local AWF service state" in uninstall_text


def test_uninstall_source_checkout_intro_names_refresh_or_config_edit_options() -> None:
    """Assert the intro gives both options before deleting a recorded checkout."""
    uninstall_text = (REPO_ROOT / "docs" / "UNINSTALL.md").read_text(encoding="utf-8")
    intro_words = " ".join(uninstall_text.split("## uv tool", maxsplit=1)[0].split())

    assert "either refresh the persisted path. The" not in intro_words
    assert (
        "Before deleting a recorded checkout, either refresh the persisted path via "
        "`awf setup --source-checkout` (stop Core first) or edit `~/.awf/config.yml` "
        "directly (the no-stop option)."
    ) in intro_words


def test_uninstall_source_checkout_refresh_requires_core_stop_guidance() -> None:
    """Assert uninstall refresh docs do not leave Core holding checked ports."""
    uninstall_text = (REPO_ROOT / "docs" / "UNINSTALL.md").read_text(encoding="utf-8")
    intro_section = uninstall_text.split("## uv tool", maxsplit=1)[0]
    core_stop_guidance = "Stop local Core before refreshing source-checkout metadata"
    port_block_guidance = (
        "`awf setup` checks the API and Postgres host ports and blocks while the previous "
        "Core stack still holds them"
    )
    no_stop_guidance = "Editing `~/.awf/config.yml` remains the no-stop option"
    no_global_wrapper_guidance = (
        "The introductory refresh example uses the no-global source-checkout wrapper"
    )
    global_tool_equivalent_guidance = (
        "Global source-checkout installs use the equivalent bare "
        "`awf setup --source-checkout ...` form"
    )
    checkout_cd_line = "cd /path/to/aira-agent-workspace-fabric"
    source_cases = (
        (
            "global tool install",
            _markdown_section(
                uninstall_text,
                "## Source Checkout With Global Tool Install",
            ),
            "awf setup --source-checkout /path/to/replacement/aira-agent-workspace-fabric",
        ),
        (
            "no global install",
            _markdown_section(
                uninstall_text,
                "## Source Checkout With No Global Install",
            ),
            (
                "uv run --python 3.12 --extra dev awf setup "
                "--source-checkout /path/to/replacement/aira-agent-workspace-fabric"
            ),
        ),
    )

    intro_words = " ".join(intro_section.split())
    assert core_stop_guidance in intro_words
    assert port_block_guidance in intro_words
    assert no_stop_guidance in intro_words
    assert no_global_wrapper_guidance in intro_words
    assert global_tool_equivalent_guidance in intro_words
    intro_setup_line = (
        "uv run --python 3.12 --extra dev awf setup "
        "--source-checkout /path/to/replacement/aira-agent-workspace-fabric"
    )
    assert (
        "\nawf setup --source-checkout /path/to/replacement/aira-agent-workspace-fabric"
        not in intro_section
    )
    intro_setup_words_index = _required_index(
        intro_words,
        intro_setup_line,
        "intro source-checkout uninstall summary",
    )
    assert intro_words.index(core_stop_guidance) < intro_setup_words_index
    assert checkout_cd_line in intro_section
    intro_setup_index = _required_index(
        intro_section,
        intro_setup_line,
        "intro source-checkout uninstall",
    )
    (
        intro_env_restore_start_index,
        intro_env_restore_end_index,
        intro_stop_start_index,
        intro_stop_end_index,
    ) = _assert_source_checkout_service_env_restore_and_stop(
        "intro source-checkout uninstall",
        intro_section,
        "refreshing source-checkout metadata",
        require_legacy_fallback=True,
    )
    assert (
        intro_section.index(checkout_cd_line)
        < intro_env_restore_start_index
        < intro_env_restore_end_index
        < intro_stop_start_index
        < intro_stop_end_index
        < intro_setup_index
    ), "intro must provide guarded Core stop commands before metadata refresh"
    for label, section, setup_line in source_cases:
        section_words = " ".join(section.split())
        assert core_stop_guidance in section_words, f"{label} must tell users to stop Core"
        assert port_block_guidance in section_words, f"{label} must explain setup port blockers"
        assert section_words.index(core_stop_guidance) < section_words.index(setup_line)
        assert checkout_cd_line in section, f"{label} must cd into the source checkout"
        (
            env_restore_start_index,
            env_restore_end_index,
            stop_start_index,
            stop_end_index,
        ) = _assert_source_checkout_service_env_restore_and_stop(
            f"{label} uninstall",
            section,
            "refreshing source-checkout metadata",
            require_legacy_fallback=True,
        )
        assert (
            section.index(checkout_cd_line)
            < env_restore_start_index
            < env_restore_end_index
            < stop_start_index
            < stop_end_index
            < section.index(setup_line)
        ), f"{label} must provide guarded Core stop commands before metadata refresh"


def test_uninstall_source_checkout_env_restore_accepts_exported_dotenv_entries(
    tmp_path: Path,
) -> None:
    """Assert uninstall snippets accept dotenv syntax AWF itself accepts."""
    uninstall_text = (REPO_ROOT / "docs" / "UNINSTALL.md").read_text(encoding="utf-8")
    env_file = tmp_path / ".env"

    for key in ("AWF_API_TOKEN", "AWF_POSTGRES_PASSWORD"):
        expressions = set(re.findall(rf"sed -n '([^']*{key}[^']*)'", uninstall_text))
        assert len(expressions) == 1
        expression = expressions.pop()
        env_file.write_text(
            "\n".join(
                (
                    f"{key}_BACKUP=keep",
                    f"  export {key}=from-export",
                    f"\t{key} = from-leading-space",
                )
            )
            + "\n",
            encoding="utf-8",
        )

        result = subprocess.run(
            ["sed", "-n", expression, str(env_file)],
            check=True,
            capture_output=True,
            text=True,
        )

        assert result.stdout.splitlines() == [
            "from-export",
            "from-leading-space",
        ]


def test_upgrade_no_global_source_checkout_rollback_uses_uv_run() -> None:
    """Assert no-global checkout rollback does not require a global awf executable."""
    upgrade_text = (REPO_ROOT / "docs" / "UPGRADE.md").read_text(encoding="utf-8")
    rollback_section = _markdown_section(upgrade_text, "## Rollback")
    no_global_heading = "For the source checkout with no global install lane"
    setup_line = 'uv run --python 3.12 --extra dev awf setup --source-checkout "$PWD"'
    no_global_commands = (
        setup_line,
        'uv run --python 3.12 --extra dev awf start --source-checkout "$PWD"',
        "uv run --python 3.12 --extra dev awf service status --format pretty",
        (
            "uv run --python 3.12 --extra dev awf smoke run --project <path> "
            "--mocked-local --format pretty"
        ),
    )

    assert no_global_heading in rollback_section
    no_global_section = rollback_section.split(no_global_heading, maxsplit=1)[1]
    (
        env_restore_start_index,
        env_restore_end_index,
        stop_start_index,
        stop_end_index,
    ) = _assert_source_checkout_service_env_restore_and_stop(
        "no-global source-checkout rollback",
        no_global_section,
        "rollback",
        require_legacy_fallback=True,
    )
    assert (
        no_global_section.index("uv sync --extra dev")
        < env_restore_start_index
        < env_restore_end_index
        < stop_start_index
        < stop_end_index
        < no_global_section.index(setup_line)
    )
    for command in no_global_commands:
        assert command in no_global_section


def test_upgrade_global_source_checkout_rollback_refreshes_metadata() -> None:
    """Assert global-tool checkout rollback refreshes persisted source metadata."""
    upgrade_text = (REPO_ROOT / "docs" / "UPGRADE.md").read_text(encoding="utf-8")
    rollback_section = _markdown_section(upgrade_text, "## Rollback")
    global_heading = "For the source checkout with global tool install lane"
    no_global_heading = "For the source checkout with no global install lane"
    setup_line = 'awf setup --source-checkout "$PWD"'
    start_line = 'awf start --source-checkout "$PWD"'
    global_commands = (
        setup_line,
        start_line,
        "awf service status --format pretty",
        "awf smoke run --project <path> --mocked-local --format pretty",
    )

    assert global_heading in rollback_section
    assert no_global_heading in rollback_section
    global_section = rollback_section.split(global_heading, maxsplit=1)[1].split(
        no_global_heading,
        maxsplit=1,
    )[0]
    assert "cd /path/to/aira-agent-workspace-fabric" in global_section
    assert "uv tool install . --force" in global_section
    (
        env_restore_start_index,
        env_restore_end_index,
        stop_start_index,
        stop_end_index,
    ) = _assert_source_checkout_service_env_restore_and_stop(
        "global source-checkout rollback",
        global_section,
        "rollback",
        require_legacy_fallback=True,
    )
    assert (
        global_section.index("uv tool install . --force")
        < env_restore_start_index
        < env_restore_end_index
        < stop_start_index
        < stop_end_index
        < global_section.index(setup_line)
        < global_section.index(start_line)
    )
    for command in global_commands:
        assert command in global_section


def test_uninstall_no_global_source_checkout_cleanup_uses_uv_run() -> None:
    """Assert no-global checkout cleanup does not require a global awf executable."""
    uninstall_text = (REPO_ROOT / "docs" / "UNINSTALL.md").read_text(encoding="utf-8")
    source_section = _markdown_section(
        uninstall_text,
        "## Source Checkout With No Global Install",
    )
    replacement_setup = (
        "uv run --python 3.12 --extra dev awf setup "
        "--source-checkout /path/to/replacement/aira-agent-workspace-fabric"
    )

    assert replacement_setup in source_section
    assert "rm -rf" in source_section
    assert source_section.index(replacement_setup) < source_section.index("rm -rf")


def test_uninstall_global_source_checkout_refreshes_before_tool_uninstall() -> None:
    """Assert global-tool checkout cleanup refreshes metadata before removing awf."""
    uninstall_text = (REPO_ROOT / "docs" / "UNINSTALL.md").read_text(encoding="utf-8")
    source_section = _markdown_section(
        uninstall_text,
        "## Source Checkout With Global Tool Install",
    )
    replacement_setup = (
        "awf setup --source-checkout /path/to/replacement/aira-agent-workspace-fabric"
    )

    assert replacement_setup in source_section
    assert "uv tool uninstall agent-workspace-fabric" in source_section
    assert "rm -rf" in source_section
    assert source_section.index(replacement_setup) < source_section.index(
        "uv tool uninstall agent-workspace-fabric"
    )
    assert source_section.index(replacement_setup) < source_section.index("rm -rf")


def test_virtualenv_lifecycle_docs_cover_readme_install_path() -> None:
    """Assert README-supported virtualenv installs have upgrade/uninstall guidance."""
    readme_text = README_PATH.read_text(encoding="utf-8")
    upgrade_text = (REPO_ROOT / "docs" / "UPGRADE.md").read_text(encoding="utf-8")
    uninstall_text = (REPO_ROOT / "docs" / "UNINSTALL.md").read_text(encoding="utf-8")

    assert "python -m venv .venv" in readme_text
    assert "pip install agent-workspace-fabric" in readme_text
    assert "## Virtualenv / pip" in upgrade_text
    assert "pip install --upgrade agent-workspace-fabric" in upgrade_text
    assert ". .venv/bin/activate" in upgrade_text
    assert "## Virtualenv / pip" in uninstall_text
    assert "pip uninstall agent-workspace-fabric" in uninstall_text
    assert "deactivate" in uninstall_text


def test_public_first_run_docs_do_not_advertise_unpublished_curl_installer() -> None:
    """Assert public first-run docs do not expose the unpublished curl lane."""
    public_first_run_paths = (
        README_PATH,
        REPO_ROOT / "docs" / "QUICKSTART.md",
        REPO_ROOT / "docs" / "GETTING_STARTED.md",
        REPO_ROOT / "docs" / "UPGRADE.md",
        REPO_ROOT / "docs" / "UNINSTALL.md",
    )

    offenders = [
        str(path.relative_to(REPO_ROOT))
        for path in public_first_run_paths
        if "curl -fsSL https://aira.pro/install.sh" in path.read_text(encoding="utf-8")
    ]

    assert not offenders


def test_public_oss_release_metadata_is_consistent() -> None:
    readme_text = README_PATH.read_text(encoding="utf-8")
    license_text = (REPO_ROOT / "LICENSE").read_text(encoding="utf-8")
    pyproject_text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    releasing_text = (REPO_ROOT / "RELEASING.md").read_text(encoding="utf-8")

    assert "# Agent Workspace Fabric (AWF)" in readme_text
    assert "[LICENSE](LICENSE)" in readme_text
    assert "Apache License" in license_text
    assert 'name = "agent-workspace-fabric"' in pyproject_text
    assert "pip-licenses" in releasing_text
    assert "license-checker" in releasing_text
    assert "awf service readiness --format json" in releasing_text


def test_public_docs_describe_supported_release_install_channels() -> None:
    public_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            REPO_ROOT / "README.md",
            REPO_ROOT / "docs" / "QUICKSTART.md",
            REPO_ROOT / "docs" / "GETTING_STARTED.md",
            REPO_ROOT / "docs" / "UPGRADE.md",
            REPO_ROOT / "RELEASING.md",
        )
    )

    assert "uv tool install agent-workspace-fabric" in public_text
    assert "pipx install agent-workspace-fabric" in public_text
    assert "python -m venv .venv" in public_text
    assert "pip install agent-workspace-fabric" in public_text
    assert "uv tool install . --force" in public_text
    assert "PyPI Trusted Publishing" in public_text
    assert "brew install agent-workspace-fabric" not in public_text
    assert "Homebrew is planned" in public_text


def test_primary_public_docs_use_public_brand_and_not_internal_backlog() -> None:
    public_entrypoints = [
        REPO_ROOT / "README.md",
        REPO_ROOT / "docs" / "README.md",
        REPO_ROOT / "docs" / "QUICKSTART.md",
        REPO_ROOT / "docs" / "GETTING_STARTED.md",
        REPO_ROOT / "CONTRIBUTING.md",
        REPO_ROOT / "RELEASING.md",
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in public_entrypoints)

    assert "Agent Workspace Fabric (AWF)" in combined
    assert "Aira Agent Workspace Fabric" not in combined
    assert "TODO/pre-gke-industrial-readiness.md" not in combined


def test_plan_protocol_and_awf_plans_readme_are_tracked() -> None:
    result = subprocess.run(
        ["git", "ls-files", "docs/awf-plans", "plans"],
        check=True,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    tracked = set(result.stdout.splitlines())

    assert "docs/awf-plans/README.md" in tracked
    assert "plans/PLAN_EXECUTION_PROTOCOL.md" in tracked


def test_shell_snippet_validation_fails_cleanly_when_bash_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _missing_bash(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("bash")

    monkeypatch.setattr(subprocess, "run", _missing_bash)

    fence = MarkdownFence(
        path="docs/example.md",
        line=12,
        language="bash",
        body="echo ok",
    )

    with pytest.raises(
        pytest.fail.Exception,
        match=r"docs/example\.md:12 cannot validate shell snippet because bash is not available on PATH",
    ):
        _assert_snippet_syntax(fence)


def test_markdown_fences_consumes_closing_fence_between_adjacent_snippets() -> None:
    text = """```bash
echo ok
```
```json
{"ok": true}
```
"""

    assert _markdown_fences("docs/example.md", text) == [
        MarkdownFence(
            path="docs/example.md",
            line=1,
            language="bash",
            body="echo ok",
        ),
        MarkdownFence(
            path="docs/example.md",
            line=4,
            language="json",
            body='{"ok": true}',
        ),
    ]


def test_markdown_fences_accepts_indented_copy_paste_fences() -> None:
    text = '1. Step\n   ```python\n   print("ok")\n   ```\n'

    assert _fence_delimiter_count_is_even(text)
    assert not _fence_delimiter_count_is_even("1. Step\n   ```python\n   print('ok')\n")
    assert _markdown_fences("docs/example.md", text) == [
        MarkdownFence(
            path="docs/example.md",
            line=2,
            language="python",
            body='print("ok")',
        ),
    ]


def test_public_docs_are_discovered_from_docs_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (tmp_path / "README.md").write_text("# Docs\n", encoding="utf-8")
    (docs_dir / "NEW_GUIDE.md").write_text("# New Guide\n", encoding="utf-8")
    (docs_dir / "PLAN_MVP.md").write_text("# Internal plan\n", encoding="utf-8")
    internal_dir = docs_dir / "awf-plans"
    internal_dir.mkdir()
    (internal_dir / "ws_private.md").write_text("# Private plan\n", encoding="utf-8")

    module = helpers
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(module, "README_PATH", tmp_path / "README.md")

    assert _public_docs() == {"docs/NEW_GUIDE.md"}


def test_root_public_docs_linked_from_readme_are_discovered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (tmp_path / "README.md").write_text(
        "See [release checklist](RELEASING.md).\n",
        encoding="utf-8",
    )
    (tmp_path / "RELEASING.md").write_text("# Release Checklist\n", encoding="utf-8")

    module = helpers
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(module, "README_PATH", tmp_path / "README.md")

    assert _public_docs() == {"RELEASING.md"}


def test_copy_paste_docs_ignore_missing_readme_linked_docs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "README.md").write_text(
        "See [missing guide](docs/MISSING.md).\n",
        encoding="utf-8",
    )

    module = helpers
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(module, "README_PATH", tmp_path / "README.md")

    assert _public_docs() == {"docs/MISSING.md"}
    assert _copy_paste_docs() == set()


def test_awf_command_mentions_ignore_missing_readme_linked_docs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "README.md").write_text(
        "See [missing guide](docs/MISSING.md).\n",
        encoding="utf-8",
    )

    module = helpers
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(module, "README_PATH", tmp_path / "README.md")

    paths = [Path("README.md"), *map(Path, sorted(_public_docs()))]

    assert _public_docs() == {"docs/MISSING.md"}
    assert _awf_command_mentions(paths) == []
