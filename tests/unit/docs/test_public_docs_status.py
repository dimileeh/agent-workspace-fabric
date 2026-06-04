from __future__ import annotations

import ast
import json
import re
import shlex
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

import pytest
import yaml

from awf.cli.main import app
from awf.service.config import DEFAULT_LOCAL_SERVICE_API_BASE_URL
from awf.service.smoke import DEFAULT_LOCAL_CONSOLE_URL

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
README_PATH = REPO_ROOT / "README.md"
DOCS_INDEX_CANDIDATES = (README_PATH, REPO_ROOT / "docs" / "README.md")

ROOT_PUBLIC_DOC_NAMES = {
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "RELEASING.md",
}
INTERNAL_DOC_PREFIXES = ("docs/awf-plans/",)
PLANNING_DOC_NAMES = {
    "docs/awf_prd_v2.2.md",
    "docs/PLAN_MVP.md",
    "docs/PLAN_PR_MONITOR.md",
    "docs/PLAN_RELEASE_PR_SYNC.md",
}
OPTIONAL_PUBLIC_GUIDES = {
    "docs/TROUBLESHOOTING.md",
}
COPY_PASTE_DOC_HINTS = {
    "docs/PROJECT_ONBOARDING.md",
}

LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\((?P<target>[^)]+)\)")
FENCE_DELIMITER_RE = re.compile(r"^ {0,3}```", re.MULTILINE)
OPENING_FENCE_RE = re.compile(
    r"^(?P<indent> {0,3})(?P<delimiter>`{3,})(?P<language>[A-Za-z0-9_+.-]*)[^\n]*$",
)
AWF_COMMAND_RE = re.compile(r"(?<![\w./-])awf(?P<tail>\s+[^`\n|;)]*)")


@dataclass(frozen=True)
class MarkdownFence:
    path: str
    line: int
    language: str
    body: str


@dataclass(frozen=True)
class AwfCommandMention:
    path: str
    command: str
    command_path: tuple[str, ...]


def test_every_public_guide_is_linked_from_docs_index_or_readme() -> None:
    public_docs = _public_docs()
    index_links = _docs_index_links()

    missing = sorted(public_docs - index_links)

    assert not missing, (
        "Public docs must be discoverable from README.md or docs/README.md. "
        f"Missing index links: {missing}"
    )


def test_awf_commands_mentioned_in_public_docs_exist_in_cli_help_tree() -> None:
    command_tree = _typer_command_tree(app)
    mentions = _awf_command_mentions([Path("README.md"), *map(Path, sorted(_public_docs()))])
    missing = [mention for mention in mentions if mention.command_path not in command_tree]

    assert not missing, (
        "Public docs mention AWF commands that are not present in the Typer command tree: "
        + ", ".join(
            f"{mention.path}: `{mention.command}` -> {' '.join(mention.command_path)}"
            for mention in missing
        )
    )


def test_copy_paste_marked_snippets_are_syntactically_valid() -> None:
    checked: list[str] = []
    for rel_path in sorted(_copy_paste_docs()):
        text = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
        assert _fence_delimiter_count_is_even(text), f"{rel_path} has an unclosed code fence"
        for fence in _markdown_fences(rel_path, text):
            _assert_snippet_syntax(fence)
            checked.append(f"{fence.path}:{fence.line}")

    assert checked, "Expected at least one copy-paste-marked snippet to validate."


def test_quickstart_is_canonical_and_not_a_stub() -> None:
    """Assert Quickstart remains the canonical public first-run entrypoint."""
    readme_text = README_PATH.read_text(encoding="utf-8")
    quickstart_text = (REPO_ROOT / "docs" / "QUICKSTART.md").read_text(encoding="utf-8")
    start_here_text = (REPO_ROOT / "docs" / "START_HERE.md").read_text(encoding="utf-8")

    assert "docs/QUICKSTART.md" in readme_text
    assert "docs/UPGRADE.md" in readme_text
    assert "docs/UNINSTALL.md" in readme_text
    assert "hosted curl installer lane is intentionally omitted" in readme_text
    assert "&#124;" not in readme_text
    assert "docs/START_HERE.md" not in readme_text
    assert "currently a stub" not in quickstart_text.lower()
    assert "awf setup" in quickstart_text
    assert "awf start" in quickstart_text
    assert "awf init <path>" in quickstart_text
    assert "awf smoke run --project" in quickstart_text
    assert "[Quickstart](QUICKSTART.md)" in start_here_text


def test_readme_first_run_grammar_reuses_initialized_project_path() -> None:
    """Assert README first-run commands are valid for each install lane."""
    readme_text = README_PATH.read_text(encoding="utf-8")
    first_run_section = _markdown_section(readme_text, "## Installation").split(
        "For the full lane-specific commands",
        maxsplit=1,
    )[0]

    assert "After installing in any lane" not in first_run_section
    assert "For package-manager and virtualenv lanes that put `awf` on `PATH`" in (
        first_run_section
    )
    assert "For the source checkout with global tool install lane" in first_run_section
    assert "For the no-global source checkout lane" in first_run_section
    assert "awf init <path>" in first_run_section
    assert "awf smoke run --project <path> --mocked-local --format pretty" in first_run_section
    assert "awf smoke run --mocked-local --format pretty" not in first_run_section
    assert 'awf setup --source-checkout "$PWD"' in first_run_section
    assert 'awf start --source-checkout "$PWD"' in first_run_section
    assert (
        'uv run --python 3.12 --extra dev awf setup --source-checkout "$PWD"' in first_run_section
    )
    assert (
        'uv run --python 3.12 --extra dev awf start --source-checkout "$PWD"' in first_run_section
    )
    assert not re.search(
        r"(?m)^uv run --python 3\.12 --extra dev awf (setup|start)\s*$",
        first_run_section,
    )
    assert "uv run --python 3.12 --extra dev awf init <path>" in first_run_section
    assert (
        "uv run --python 3.12 --extra dev awf smoke run --project <path> "
        "--mocked-local --format pretty"
    ) in first_run_section
    global_source_section = first_run_section.split(
        "For the source checkout with global tool install lane",
        maxsplit=1,
    )[1].split("For the no-global source checkout lane", maxsplit=1)[0]
    assert not re.search(r"(?m)^awf (setup|start)\s*$", global_source_section)


def test_quickstart_presents_available_complete_first_run_lanes() -> None:
    """Assert each advertised Quickstart lane is complete and currently available."""
    quickstart_text = (REPO_ROOT / "docs" / "QUICKSTART.md").read_text(encoding="utf-8")
    lanes = {
        "## Lane 1: uv tool or pipx": ("release-installed", "package-manager"),
        "## Lane 2: Source Checkout With Global Tool Install": (
            "inspectable source",
            "global tool",
        ),
        "## Lane 3: Source Checkout With No Global Install": (
            "inspectable source",
            "no global install",
        ),
    }

    for heading, descriptors in lanes.items():
        section = _markdown_section(quickstart_text, heading)
        for descriptor in descriptors:
            assert descriptor in section
        assert "awf setup" in section
        assert "awf start" in section
        assert ("awf init <path>" in section) or re.search(r"(?m)^awf init \.\s*$", section)
        assert "smoke run" in section
        assert "--mocked-local --format pretty" in section
        assert "Upgrade:" in section
        assert "Uninstall:" in section

    assert "## Lane 1: Curl Installer" not in quickstart_text
    assert "curl -fsSL https://aira.pro/install.sh | sh" not in quickstart_text
    assert re.search(
        r"hosted curl\s+installer lane is intentionally omitted",
        quickstart_text,
    ), "Expected hosted curl installer omission note in docs/QUICKSTART.md"
    assert "uv tool install agent-workspace-fabric" in quickstart_text
    assert "pipx install agent-workspace-fabric" in quickstart_text
    assert "uv tool install . --force" in quickstart_text
    assert "uv run --python 3.12 --extra dev awf setup" in quickstart_text
    assert "AWF_SETUP_PLACEHOLDER" not in quickstart_text
    assert "AWF_START_PLACEHOLDER" not in quickstart_text
    assert not re.search(r"(?m)^awf service bootstrap\s*$", quickstart_text)


def test_quickstart_keeps_package_manager_alternatives_in_separate_blocks() -> None:
    """Assert copying one Lane 1 bash block cannot execute both install managers."""
    quickstart_text = (REPO_ROOT / "docs" / "QUICKSTART.md").read_text(encoding="utf-8")
    lane_section = _markdown_section(quickstart_text, "## Lane 1: uv tool or pipx")
    alternative_pairs = (
        ("uv tool install agent-workspace-fabric", "pipx install agent-workspace-fabric"),
        ("uv tool upgrade agent-workspace-fabric", "pipx upgrade agent-workspace-fabric"),
        (
            "uv tool uninstall agent-workspace-fabric",
            "pipx uninstall agent-workspace-fabric",
        ),
    )
    mixed_blocks: list[str] = []

    for fence in _markdown_fences("docs/QUICKSTART.md", lane_section):
        if fence.language != "bash":
            continue

        executable_lines = {
            line.strip()
            for line in fence.body.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        for uv_command, pipx_command in alternative_pairs:
            if uv_command in executable_lines and pipx_command in executable_lines:
                mixed_blocks.append(
                    f"{fence.path}:{fence.line} mixes `{uv_command}` and `{pipx_command}`"
                )

    assert not mixed_blocks, "; ".join(mixed_blocks)


def test_quickstart_package_first_run_persists_service_env_for_upgrade() -> None:
    """Assert package first-run secrets remain available to later upgrades."""
    quickstart_text = (REPO_ROOT / "docs" / "QUICKSTART.md").read_text(encoding="utf-8")
    lane_section = _markdown_section(quickstart_text, "## Lane 1: uv tool or pipx")
    first_run_section = lane_section.split("\nUpgrade:\n", maxsplit=1)[0]
    api_export = 'export AWF_API_TOKEN="$(openssl rand -hex 32)"'
    password_export = 'export AWF_POSTGRES_PASSWORD="${AWF_POSTGRES_PASSWORD:-awf_dev}"'
    host_port_export = 'export AWF_POSTGRES_HOST_PORT="${AWF_POSTGRES_HOST_PORT:-5433}"'
    database_url_export = (
        'export AWF_DATABASE_URL="postgresql+asyncpg://awf:${AWF_POSTGRES_PASSWORD}'
        '@localhost:${AWF_POSTGRES_HOST_PORT}/awf"'
    )
    api_persist = "  printf 'AWF_API_TOKEN=%s\\n' \"$AWF_API_TOKEN\""
    password_persist = "  printf 'AWF_POSTGRES_PASSWORD=%s\\n' \"$AWF_POSTGRES_PASSWORD\""
    host_port_persist = "  printf 'AWF_POSTGRES_HOST_PORT=%s\\n' \"$AWF_POSTGRES_HOST_PORT\""
    database_url_persist = "  printf 'AWF_DATABASE_URL=%s\\n' \"$AWF_DATABASE_URL\""
    env_tmp = 'awf_env_tmp="$(mktemp)"'
    preserve_existing_env = "\n".join(
        (
            "    sed \\",
            "      -e '/^[[:space:]]*\\(export[[:space:]][[:space:]]*\\)\\{0,1\\}AWF_API_TOKEN[[:space:]]*=/d' \\",
            "      -e '/^[[:space:]]*\\(export[[:space:]][[:space:]]*\\)\\{0,1\\}AWF_POSTGRES_PASSWORD[[:space:]]*=/d' \\",
            "      -e '/^[[:space:]]*\\(export[[:space:]][[:space:]]*\\)\\{0,1\\}AWF_POSTGRES_HOST_PORT[[:space:]]*=/d' \\",
            "      -e '/^[[:space:]]*\\(export[[:space:]][[:space:]]*\\)\\{0,1\\}AWF_DATABASE_URL[[:space:]]*=/d' \\",
            "      .env",
        )
    )
    gnu_only_sed_alternation = (
        "sed '/^\\(AWF_API_TOKEN\\|AWF_POSTGRES_PASSWORD\\|AWF_POSTGRES_HOST_PORT"
        "\\|AWF_DATABASE_URL\\)=/d' .env"
    )
    persist_target = 'mv "$awf_env_tmp" .env'
    unsafe_persist_target = "} > .env"
    setup_command = "\nawf setup\n"

    assert "persist" in first_run_section.lower()
    assert api_export in first_run_section
    assert password_export in first_run_section
    assert host_port_export in first_run_section
    assert database_url_export in first_run_section
    assert env_tmp in first_run_section
    assert api_persist in first_run_section
    assert password_persist in first_run_section
    assert host_port_persist in first_run_section
    assert database_url_persist in first_run_section
    assert preserve_existing_env in first_run_section
    assert gnu_only_sed_alternation not in first_run_section
    assert persist_target in first_run_section
    assert unsafe_persist_target not in first_run_section
    assert setup_command in first_run_section
    assert (
        first_run_section.index(api_export)
        < first_run_section.index(password_export)
        < first_run_section.index(host_port_export)
        < first_run_section.index(database_url_export)
        < first_run_section.index(env_tmp)
        < first_run_section.index(api_persist)
        < first_run_section.index(password_persist)
        < first_run_section.index(host_port_persist)
        < first_run_section.index(database_url_persist)
        < first_run_section.index(preserve_existing_env)
        < first_run_section.index(persist_target)
        < first_run_section.index(setup_command)
    )


def test_quickstart_package_first_run_strips_exported_awf_env_entries(
    tmp_path: Path,
) -> None:
    """Assert Quickstart replaces exported or whitespace-padded AWF env entries."""
    quickstart_text = (REPO_ROOT / "docs" / "QUICKSTART.md").read_text(encoding="utf-8")
    lane_section = _markdown_section(quickstart_text, "## Lane 1: uv tool or pipx")
    first_run_section = lane_section.split("\nUpgrade:\n", maxsplit=1)[0]
    sed_expressions = re.findall(r"^\s+-e '([^']+)'\s*\\$", first_run_section, re.MULTILINE)
    assert len(sed_expressions) == 4

    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            (
                "export AWF_API_TOKEN=old-token",
                " export AWF_POSTGRES_PASSWORD=old-password",
                "\tAWF_POSTGRES_HOST_PORT = 15432",
                "export AWF_DATABASE_URL = old-url",
                "PROVIDER_TOKEN=keep",
                "AWF_API_TOKEN_BACKUP=keep",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    command = ["sed"]
    for expression in sed_expressions:
        command.extend(("-e", expression))
    command.append(str(env_file))

    result = subprocess.run(command, check=True, capture_output=True, text=True)

    assert result.stdout.splitlines() == [
        "PROVIDER_TOKEN=keep",
        "AWF_API_TOKEN_BACKUP=keep",
    ]


@pytest.mark.parametrize(
    ("heading", "setup_command"),
    (
        (
            "## Lane 2: Source Checkout With Global Tool Install",
            'awf setup --source-checkout "$PWD"',
        ),
        (
            "## Lane 3: Source Checkout With No Global Install",
            'uv run --python 3.12 --extra dev awf setup --source-checkout "$PWD"',
        ),
    ),
)
def test_quickstart_source_checkout_first_run_persists_compose_env_for_upgrade(
    heading: str,
    setup_command: str,
) -> None:
    """Assert source-checkout first-run secrets remain available to later upgrades."""
    quickstart_text = (REPO_ROOT / "docs" / "QUICKSTART.md").read_text(encoding="utf-8")
    lane_section = _markdown_section(quickstart_text, heading)
    first_run_section = lane_section.split("\nUpgrade:\n", maxsplit=1)[0]
    api_export = 'export AWF_API_TOKEN="$(openssl rand -hex 32)"'
    password_export = 'export AWF_POSTGRES_PASSWORD="${AWF_POSTGRES_PASSWORD:-awf_dev}"'
    host_port_export = 'export AWF_POSTGRES_HOST_PORT="${AWF_POSTGRES_HOST_PORT:-5433}"'
    database_url_export = (
        'export AWF_DATABASE_URL="postgresql+asyncpg://awf:${AWF_POSTGRES_PASSWORD}'
        '@localhost:${AWF_POSTGRES_HOST_PORT}/awf"'
    )
    api_persist = "  printf 'AWF_API_TOKEN=%s\\n' \"$AWF_API_TOKEN\""
    password_persist = "  printf 'AWF_POSTGRES_PASSWORD=%s\\n' \"$AWF_POSTGRES_PASSWORD\""
    host_port_persist = "  printf 'AWF_POSTGRES_HOST_PORT=%s\\n' \"$AWF_POSTGRES_HOST_PORT\""
    database_url_persist = "  printf 'AWF_DATABASE_URL=%s\\n' \"$AWF_DATABASE_URL\""
    persist_target = "} > docker/compose/.env"

    assert "persist" in first_run_section.lower()
    assert api_export in first_run_section
    assert password_export in first_run_section
    assert host_port_export in first_run_section
    assert database_url_export in first_run_section
    assert api_persist in first_run_section
    assert password_persist in first_run_section
    assert host_port_persist in first_run_section
    assert database_url_persist in first_run_section
    assert persist_target in first_run_section
    assert f"\n{setup_command}\n" in first_run_section
    assert (
        first_run_section.index(api_export)
        < first_run_section.index(password_export)
        < first_run_section.index(host_port_export)
        < first_run_section.index(database_url_export)
        < first_run_section.index(api_persist)
        < first_run_section.index(password_persist)
        < first_run_section.index(host_port_persist)
        < first_run_section.index(database_url_persist)
        < first_run_section.index(persist_target)
        < first_run_section.index(f"\n{setup_command}\n")
    )


def test_quickstart_smoke_commands_reuse_initialized_project_paths() -> None:
    """Assert Quickstart smoke commands validate the lane's initialized project."""
    quickstart_text = (REPO_ROOT / "docs" / "QUICKSTART.md").read_text(encoding="utf-8")

    smoke_lines = [line for line in quickstart_text.splitlines() if "smoke run" in line]

    assert smoke_lines
    assert all("--project " in line for line in smoke_lines)


def test_quickstart_mocked_smoke_keeps_github_auth_optional() -> None:
    """Assert mocked first-run commands do not require GitHub CLI auth."""
    quickstart_text = (REPO_ROOT / "docs" / "QUICKSTART.md").read_text(encoding="utf-8")
    lane_headings = re.findall(r"(?m)^## Lane [^\n]+", quickstart_text)
    optional_token_comment = (
        "# [optional] Only needed for PR creation/monitoring; skip for mocked smoke."
    )
    manual_token_comment = (
        "# Provide AWF_GITHUB_TOKEN, GH_TOKEN, or GITHUB_TOKEN manually if needed."
    )

    assert "does not require live GitHub or provider access" in quickstart_text
    assert "gh auth token" not in quickstart_text
    assert not re.search(
        r'(?m)^export AWF_GITHUB_TOKEN="\$\(gh auth token\)"$',
        quickstart_text,
    )
    optional_token_comment_count = quickstart_text.count(optional_token_comment)
    manual_token_comment_count = quickstart_text.count(manual_token_comment)

    assert len(lane_headings) >= 3
    assert optional_token_comment_count == len(lane_headings), (
        "Expected one optional GitHub token scope comment per Quickstart lane; "
        f"found {optional_token_comment_count} comments for {len(lane_headings)} lanes: "
        f"{lane_headings}"
    )
    assert manual_token_comment_count == len(lane_headings), (
        "Expected one manual GitHub token guidance comment per Quickstart lane; "
        f"found {manual_token_comment_count} comments for {len(lane_headings)} lanes: "
        f"{lane_headings}"
    )
    for heading in lane_headings:
        section = _markdown_section(quickstart_text, heading)
        assert optional_token_comment in section, f"{heading} is missing optional token scope"
        assert manual_token_comment in section, f"{heading} is missing manual token guidance"


def test_quickstart_clears_source_checkout_metadata_before_checkout_deletion() -> None:
    """Assert Quickstart does not leave source-checkout metadata stale."""
    quickstart_text = (REPO_ROOT / "docs" / "QUICKSTART.md").read_text(encoding="utf-8")
    core_stop_guidance = "Stop local Core before refreshing source-checkout metadata"
    port_block_guidance = (
        "`awf setup` checks the API and Postgres host ports and blocks while the previous "
        "Core stack still holds them"
    )
    no_stop_guidance = "Editing `~/.awf/config.yml` remains the no-stop option"
    stop_guard_line = "if [ -f docker/compose/.env ]; then"
    stop_env_file_line = (
        "  docker compose --env-file docker/compose/.env -f docker/compose/local-service.yml stop"
    )
    stop_fallback_line = "  docker compose -f docker/compose/local-service.yml stop"
    stop_guard_end_line = "\nfi\n"
    source_lane_headings = (
        "## Lane 2: Source Checkout With Global Tool Install",
        "## Lane 3: Source Checkout With No Global Install",
    )

    for heading in source_lane_headings:
        source_section = _markdown_section(quickstart_text, heading)
        uninstall_section = source_section.split("\nUninstall:\n", maxsplit=1)[1]
        section_words = " ".join(uninstall_section.split())
        replacement_setup = (
            "awf setup --source-checkout /path/to/replacement/aira-agent-workspace-fabric"
        )
        assert "~/.awf/config.yml" in uninstall_section
        assert "remove only the top-level `source_checkout:` block" in uninstall_section
        assert replacement_setup in uninstall_section
        assert core_stop_guidance in section_words
        assert port_block_guidance in section_words
        assert no_stop_guidance in section_words
        env_restore_start_index, env_restore_end_index = (
            _assert_source_checkout_service_env_restore_before_stop(
                f"{heading} uninstall",
                uninstall_section,
                "refreshing source-checkout metadata",
            )
        )
        assert stop_guard_line in uninstall_section
        assert stop_env_file_line in uninstall_section
        assert stop_fallback_line in uninstall_section
        assert stop_guard_end_line in uninstall_section
        assert "rm -rf aira-agent-workspace-fabric" in uninstall_section
        stop_fallback_index = uninstall_section.index(stop_fallback_line)
        assert section_words.index(core_stop_guidance) < section_words.index(port_block_guidance)
        assert (
            env_restore_start_index
            < env_restore_end_index
            < uninstall_section.index(stop_guard_line)
            < uninstall_section.index(stop_env_file_line)
            < stop_fallback_index
            < _required_index(
                uninstall_section,
                stop_guard_end_line,
                f"{heading} uninstall",
                start=stop_fallback_index,
            )
            < uninstall_section.index(replacement_setup)
        )
        assert uninstall_section.index("~/.awf/config.yml") < uninstall_section.index(
            "rm -rf aira-agent-workspace-fabric"
        )
        if heading == "## Lane 2: Source Checkout With Global Tool Install":
            assert "uv tool uninstall agent-workspace-fabric" in uninstall_section
            assert uninstall_section.index(replacement_setup) < uninstall_section.index(
                "uv tool uninstall agent-workspace-fabric"
            )


def test_source_checkout_upgrade_docs_refresh_persisted_metadata() -> None:
    """Assert source-checkout upgrades refresh persisted asset metadata."""
    quickstart_text = (REPO_ROOT / "docs" / "QUICKSTART.md").read_text(encoding="utf-8")
    upgrade_text = (REPO_ROOT / "docs" / "UPGRADE.md").read_text(encoding="utf-8")
    stop_guard_line = "if [ -f docker/compose/.env ]; then"
    stop_env_file_line = (
        "  docker compose --env-file docker/compose/.env -f docker/compose/local-service.yml stop"
    )
    stop_fallback_line = "  docker compose -f docker/compose/local-service.yml stop"
    stop_guard_end_line = "\nfi\n"
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
        assert refresh_prereq in section, f"{label} is missing upgrade prerequisite"
        env_restore_start_index, env_restore_end_index = (
            _assert_source_checkout_service_env_restore_before_stop(
                label,
                section,
                "upgrading",
            )
        )
        assert stop_guard_line in section, f"{label} must guard optional compose env file"
        assert stop_env_file_line in section, f"{label} must reuse compose env file if present"
        assert stop_fallback_line in section, f"{label} must stop Core without compose env file"
        assert stop_guard_end_line in section, f"{label} must close compose env guard"
        assert setup_line in section, f"{label} does not refresh source_checkout metadata"
        assert start_line in section, f"{label} is missing source-checkout start"
        stop_fallback_index = section.index(stop_fallback_line)
        assert (
            section.index(refresh_prereq)
            < env_restore_start_index
            < env_restore_end_index
            < section.index(stop_guard_line)
            < section.index(stop_env_file_line)
            < stop_fallback_index
            < _required_index(section, stop_guard_end_line, label, start=stop_fallback_index)
            < section.index(setup_line)
            < section.index(start_line)
        ), f"{label} must guard env-file stop, refresh metadata, then start"


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


def test_package_upgrade_env_restore_detects_only_closing_fi_keyword() -> None:
    """Assert lowercase fi in unrelated text is not treated as a shell keyword."""
    upgrade_line = "pipx upgrade agent-workspace-fabric"
    section = (
        "\n"
        + "\n".join(
            [
                upgrade_line,
                "if ! grep -q '^AWF_API_TOKEN=.' .env 2>/dev/null; then",
                (
                    '  : "${AWF_API_TOKEN:?restore the AWF_API_TOKEN used for the running '
                    'local Core or persist it in .env before upgrading}"'
                ),
                "  export AWF_API_TOKEN",
                "  # awf_config_file can be configured elsewhere",
                "if ! grep -q '^AWF_POSTGRES_PASSWORD=.' .env 2>/dev/null; then",
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

    with pytest.raises(AssertionError, match="must restore missing service env before restart"):
        _assert_package_upgrade_restores_service_env("example", section, upgrade_line)


def test_package_upgrade_env_restore_matches_restart_command_line() -> None:
    """Assert prose mentions of awf start do not satisfy restart command checks."""
    upgrade_line = "pipx upgrade agent-workspace-fabric"
    section = "\n".join(
        [
            upgrade_line,
            "if ! grep -q '^AWF_API_TOKEN=.' .env 2>/dev/null; then",
            (
                '  : "${AWF_API_TOKEN:?restore the AWF_API_TOKEN used for the running '
                'local Core or persist it in .env before upgrading}"'
            ),
            "  export AWF_API_TOKEN",
            "fi",
            "if ! grep -q '^AWF_POSTGRES_PASSWORD=.' .env 2>/dev/null; then",
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
                "if ! grep -q '^AWF_API_TOKEN=.' .env 2>/dev/null; then",
                (
                    '  : "${AWF_API_TOKEN:?restore the AWF_API_TOKEN used for the running '
                    'local Core or persist it in .env before upgrading}"'
                ),
                "  export AWF_API_TOKEN_BACKUP",
                "fi",
                "if ! grep -q '^AWF_POSTGRES_PASSWORD=.' .env 2>/dev/null; then",
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

    with pytest.raises(AssertionError, match="missing shell line"):
        _assert_package_upgrade_restores_service_env("example", section, upgrade_line)


def test_upgrade_release_installed_rollback_restores_service_env_before_start() -> None:
    """Assert release-installed rollback keeps mandatory service env available."""
    upgrade_text = (REPO_ROOT / "docs" / "UPGRADE.md").read_text(encoding="utf-8")
    rollback_section = _markdown_section(upgrade_text, "## Rollback")
    release_heading = "For release-installed lanes"
    source_heading = "For the source checkout with global tool install lane"
    api_guard_line = "if ! grep -q '^AWF_API_TOKEN=.' .env 2>/dev/null; then"
    api_require_line = (
        '  : "${AWF_API_TOKEN:?restore the AWF_API_TOKEN used for the running local Core '
        'or persist it in .env before rollback}"'
    )
    api_export_line = "  export AWF_API_TOKEN"
    unsafe_api_generation_line = (
        '  export AWF_API_TOKEN="${AWF_API_TOKEN:-$(openssl rand -hex 32)}"'
    )
    password_guard_line = "if ! grep -q '^AWF_POSTGRES_PASSWORD=.' .env 2>/dev/null; then"
    password_require_line = (
        '  : "${AWF_POSTGRES_PASSWORD:?restore the AWF_POSTGRES_PASSWORD used for '
        'the running local Core or persist it in .env before rollback}"'
    )
    password_export_line = "  export AWF_POSTGRES_PASSWORD"
    unsafe_password_default_line = (
        '  export AWF_POSTGRES_PASSWORD="${AWF_POSTGRES_PASSWORD:-awf_dev}"'
    )
    start_line = "\nawf start\n"

    assert release_heading in rollback_section
    assert source_heading in rollback_section
    release_section = rollback_section.split(release_heading, maxsplit=1)[1].split(
        source_heading,
        maxsplit=1,
    )[0]
    assert api_guard_line in release_section
    assert api_require_line in release_section
    assert api_export_line in release_section
    assert unsafe_api_generation_line not in release_section
    assert password_guard_line in release_section
    assert password_require_line in release_section
    assert password_export_line in release_section
    assert unsafe_password_default_line not in release_section
    assert start_line in release_section

    api_guard_index = release_section.index(api_guard_line)
    api_require_index = release_section.index(api_require_line)
    api_export_index = release_section.index(api_export_line)
    api_guard_end_index = _shell_closing_fi_index(
        release_section,
        api_export_index,
        "release-installed rollback",
    )
    password_guard_index = release_section.index(password_guard_line)
    password_require_index = release_section.index(password_require_line)
    password_export_index = release_section.index(password_export_line)
    password_guard_end_index = _shell_closing_fi_index(
        release_section,
        password_export_index,
        "release-installed rollback",
    )
    assert (
        api_guard_index
        < api_require_index
        < api_export_index
        < api_guard_end_index
        < password_guard_index
        < password_require_index
        < password_export_index
        < password_guard_end_index
        < release_section.index(start_line)
    ), "release-installed rollback must restore missing service env before start"


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


def test_quickstart_first_run_urls_match_smoke_defaults() -> None:
    """Assert Quickstart local URLs match the default smoke probe targets."""
    quickstart_text = (REPO_ROOT / "docs" / "QUICKSTART.md").read_text(encoding="utf-8")
    api_url = DEFAULT_LOCAL_SERVICE_API_BASE_URL
    readyz_url = f"{api_url.rstrip('/')}/readyz"
    console_url = DEFAULT_LOCAL_CONSOLE_URL

    assert f"`{api_url}` by default" in quickstart_text
    assert f"`{console_url}` when the console is running" in quickstart_text
    assert f"`{console_url}` for the console" in quickstart_text
    assert f"`{readyz_url}`" in quickstart_text
    assert "http://127.0.0.1:8000" not in quickstart_text
    assert "http://127.0.0.1:3000" not in quickstart_text


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
    assert re.search(r"`awf start`\s+uses", configure_section)
    assert "`awf service bootstrap` uses" not in configure_section
    assert "run `awf start`" in configure_section


def test_getting_started_first_run_persists_service_env_for_upgrade() -> None:
    """Assert Getting Started first-run secrets remain available to later upgrades."""
    getting_started_text = (REPO_ROOT / "docs" / "GETTING_STARTED.md").read_text(
        encoding="utf-8",
    )
    startup_section = getting_started_text.split(
        "### Recommended First-Run Sequence",
        maxsplit=1,
    )[1].split("### Configure Environment", maxsplit=1)[0]
    package_heading = "For package-manager or virtualenv installs:"
    source_global_heading = (
        "For a source checkout with a global `awf` executable, run from the checkout:"
    )
    source_no_global_heading = (
        "For a source checkout with no global install, run from the checkout:"
    )
    api_export = 'export AWF_API_TOKEN="$(openssl rand -hex 32)"'
    password_export = 'export AWF_POSTGRES_PASSWORD="${AWF_POSTGRES_PASSWORD:-awf_dev}"'
    host_port_export = 'export AWF_POSTGRES_HOST_PORT="${AWF_POSTGRES_HOST_PORT:-5433}"'
    database_url_export = (
        'export AWF_DATABASE_URL="postgresql+asyncpg://awf:'
        '${AWF_POSTGRES_PASSWORD}@localhost:${AWF_POSTGRES_HOST_PORT}/awf"'
    )
    api_persist = "  printf 'AWF_API_TOKEN=%s\\n' \"$AWF_API_TOKEN\""
    password_persist = "  printf 'AWF_POSTGRES_PASSWORD=%s\\n' \"$AWF_POSTGRES_PASSWORD\""
    host_port_persist = "  printf 'AWF_POSTGRES_HOST_PORT=%s\\n' \"$AWF_POSTGRES_HOST_PORT\""
    database_url_persist = "  printf 'AWF_DATABASE_URL=%s\\n' \"$AWF_DATABASE_URL\""
    env_tmp = 'awf_env_tmp="$(mktemp)"'
    preserve_existing_env = "\n".join(
        (
            "    sed \\",
            "      -e '/^[[:space:]]*\\(export[[:space:]][[:space:]]*\\)\\{0,1\\}AWF_API_TOKEN[[:space:]]*=/d' \\",
            "      -e '/^[[:space:]]*\\(export[[:space:]][[:space:]]*\\)\\{0,1\\}AWF_POSTGRES_PASSWORD[[:space:]]*=/d' \\",
            "      -e '/^[[:space:]]*\\(export[[:space:]][[:space:]]*\\)\\{0,1\\}AWF_POSTGRES_HOST_PORT[[:space:]]*=/d' \\",
            "      -e '/^[[:space:]]*\\(export[[:space:]][[:space:]]*\\)\\{0,1\\}AWF_DATABASE_URL[[:space:]]*=/d' \\",
            "      .env",
        )
    )
    gnu_only_sed_alternation = (
        "sed '/^\\(AWF_API_TOKEN\\|AWF_POSTGRES_PASSWORD\\|AWF_POSTGRES_HOST_PORT"
        "\\|AWF_DATABASE_URL\\)=/d' .env"
    )
    unsafe_package_persist_target = "} > .env"

    assert package_heading in startup_section
    assert source_global_heading in startup_section
    assert source_no_global_heading in startup_section
    cases = (
        (
            "package-manager or virtualenv installs",
            startup_section.split(package_heading, maxsplit=1)[1].split(
                source_global_heading,
                maxsplit=1,
            )[0],
            'mv "$awf_env_tmp" .env',
            "\nawf setup\n",
        ),
        (
            "source checkout with global executable",
            startup_section.split(source_global_heading, maxsplit=1)[1].split(
                source_no_global_heading,
                maxsplit=1,
            )[0],
            "} > docker/compose/.env",
            '\nawf setup --source-checkout "$PWD"\n',
        ),
        (
            "source checkout with no global install",
            startup_section.split(source_no_global_heading, maxsplit=1)[1],
            "} > docker/compose/.env",
            '\nuv run --python 3.12 --extra dev awf setup --source-checkout "$PWD"\n',
        ),
    )

    for label, section, persist_target, setup_command in cases:
        assert "persist" in section.lower(), f"{label} should explain env persistence"
        assert api_export in section, f"{label} is missing AWF_API_TOKEN generation"
        assert password_export in section, f"{label} is missing AWF_POSTGRES_PASSWORD generation"
        assert host_port_export in section, f"{label} is missing AWF_POSTGRES_HOST_PORT default"
        assert database_url_export in section, (
            f"{label} must derive AWF_DATABASE_URL from AWF_POSTGRES_PASSWORD"
        )
        assert api_persist in section, f"{label} must persist AWF_API_TOKEN"
        assert password_persist in section, f"{label} must persist AWF_POSTGRES_PASSWORD"
        assert host_port_persist in section, f"{label} must persist AWF_POSTGRES_HOST_PORT"
        assert database_url_persist in section, f"{label} must persist AWF_DATABASE_URL"
        if label == "package-manager or virtualenv installs":
            assert env_tmp in section, f"{label} must write through a temporary file"
            assert preserve_existing_env in section, f"{label} must preserve existing .env entries"
            assert gnu_only_sed_alternation not in section, (
                f"{label} must not use GNU-only sed alternation"
            )
            assert unsafe_package_persist_target not in section, (
                f"{label} must not truncate existing .env entries"
            )
            assert (
                section.index(database_url_export)
                < section.index(env_tmp)
                < section.index(api_persist)
                < section.index(preserve_existing_env)
            ), f"{label} must prepare the temp file before preserving .env entries"
        assert persist_target in section, f"{label} must write the expected env file"
        assert (
            section.index(api_export)
            < section.index(password_export)
            < section.index(host_port_export)
            < section.index(database_url_export)
            < section.index(api_persist)
            < section.index(password_persist)
            < section.index(host_port_persist)
            < section.index(database_url_persist)
            < section.index(persist_target)
            < section.index(setup_command)
        ), f"{label} must persist service env before setup"


def test_getting_started_package_first_run_strips_exported_awf_env_entries(
    tmp_path: Path,
) -> None:
    """Assert Getting Started replaces exported or whitespace-padded AWF env entries."""
    getting_started_text = (REPO_ROOT / "docs" / "GETTING_STARTED.md").read_text(
        encoding="utf-8",
    )
    startup_section = getting_started_text.split(
        "### Recommended First-Run Sequence",
        maxsplit=1,
    )[1].split("### Configure Environment", maxsplit=1)[0]
    package_section = startup_section.split(
        "For package-manager or virtualenv installs:",
        maxsplit=1,
    )[1].split(
        "For a source checkout with a global `awf` executable, run from the checkout:",
        maxsplit=1,
    )[0]
    sed_expressions = re.findall(r"^\s+-e '([^']+)'\s*\\$", package_section, re.MULTILINE)
    assert len(sed_expressions) == 4

    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            (
                "export AWF_API_TOKEN=old-token",
                " export AWF_POSTGRES_PASSWORD=old-password",
                "\tAWF_POSTGRES_HOST_PORT = 15432",
                "export AWF_DATABASE_URL = old-url",
                "PROVIDER_TOKEN=keep",
                "AWF_API_TOKEN_BACKUP=keep",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    command = ["sed"]
    for expression in sed_expressions:
        command.extend(("-e", expression))
    command.append(str(env_file))

    result = subprocess.run(command, check=True, capture_output=True, text=True)

    assert result.stdout.splitlines() == [
        "PROVIDER_TOKEN=keep",
        "AWF_API_TOKEN_BACKUP=keep",
    ]


def test_getting_started_mocked_smoke_keeps_github_auth_optional() -> None:
    """Assert Getting Started first-run smoke does not require GitHub CLI auth."""
    getting_started_text = (REPO_ROOT / "docs" / "GETTING_STARTED.md").read_text(encoding="utf-8")
    startup_section = getting_started_text.split(
        "### Recommended First-Run Sequence",
        maxsplit=1,
    )[1].split("### Configure Environment", maxsplit=1)[0]

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


def test_getting_started_cli_host_port_derivation_matches_cli_default() -> None:
    """Assert Getting Started documents the CLI's localhost host-port derivation."""
    getting_started_text = (REPO_ROOT / "docs" / "GETTING_STARTED.md").read_text(encoding="utf-8")
    configure_section = getting_started_text.split(
        "### Configure Environment",
        maxsplit=1,
    )[1].split("### Local vs Production Configuration", maxsplit=1)[0]

    assert (
        "`AWF_API_HOST_PORT` is present, the CLI derives `http://localhost:<port>`"
        in configure_section
    )
    assert 'export AWF_BASE_URL="http://localhost:${AWF_API_HOST_PORT}"' in configure_section
    assert (
        "`AWF_API_HOST_PORT` is present, the CLI derives `http://127.0.0.1:<port>`"
        not in configure_section
    )


def test_mcp_setup_prerequisites_use_runnable_startup_path() -> None:
    """Assert MCP setup prerequisites use the canonical local startup flow."""
    mcp_setup_text = (REPO_ROOT / "docs" / "MCP_SETUP.md").read_text(encoding="utf-8")
    prerequisites_section = mcp_setup_text.split("## Prerequisites", maxsplit=1)[1].split(
        "## Claude Code",
        maxsplit=1,
    )[0]
    database_url_export = (
        'export AWF_DATABASE_URL="postgresql+asyncpg://awf:'
        '${AWF_POSTGRES_PASSWORD}@localhost:${AWF_POSTGRES_HOST_PORT}/awf"'
    )

    assert len(re.findall(r"(?m)^awf setup\s*$", prerequisites_section)) == 1
    assert len(re.findall(r"(?m)^awf start\s*$", prerequisites_section)) == 1
    assert (
        len(
            re.findall(
                r'(?m)^awf setup --source-checkout "\$PWD"\s*$',
                prerequisites_section,
            )
        )
        == 1
    )
    assert (
        len(
            re.findall(
                r'(?m)^awf start --source-checkout "\$PWD"\s*$',
                prerequisites_section,
            )
        )
        == 1
    )
    assert (
        len(re.findall(r"(?m)^awf service status --format pretty\s*$", prerequisites_section)) == 2
    )
    assert re.search(
        r"(?m)^} > \.env\s*\nawf setup\nawf start\nawf service status --format pretty$",
        prerequisites_section,
    )
    assert re.search(
        (
            r'(?m)^} > docker/compose/\.env\s*\nawf setup --source-checkout "\$PWD"\n'
            r'awf start --source-checkout "\$PWD"\nawf service status --format pretty$'
        ),
        prerequisites_section,
    )
    assert prerequisites_section.count(database_url_export) == 2, (
        "MCP setup package and source-checkout snippets must match first-run docs"
    )
    assert "@127.0.0.1:${AWF_POSTGRES_HOST_PORT}/awf" not in prerequisites_section
    assert not re.search(r"(?m)^awf service bootstrap\s*$", prerequisites_section)


def test_project_onboarding_first_run_uses_runnable_startup_path() -> None:
    """Assert onboarding starts AWF before initializing the project workspace."""
    onboarding_text = (REPO_ROOT / "docs" / "PROJECT_ONBOARDING.md").read_text(encoding="utf-8")
    first_run_section = onboarding_text.split(
        "## First-run operator command",
        maxsplit=1,
    )[1].split("## One-message prompt", maxsplit=1)[0]

    assert (
        'export AWF_API_TOKEN="$(openssl rand -hex 32)"\n'
        'export AWF_POSTGRES_PASSWORD="${AWF_POSTGRES_PASSWORD:-awf_dev}"\n'
        'export AWF_GITHUB_TOKEN="$(gh auth token)"\n'
        "awf setup\n"
        "awf start\n"
        "awf init ."
    ) in first_run_section
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

    assert (REPO_ROOT / "CHANGELOG.md").exists()
    assert (REPO_ROOT / "docs" / "UPGRADE.md").exists()
    assert (REPO_ROOT / "docs" / "UNINSTALL.md").exists()
    assert (REPO_ROOT / "RELEASING.md").exists()
    assert "[Changelog](CHANGELOG.md)" in readme_text
    assert "[Upgrade Guide](docs/UPGRADE.md)" in readme_text
    assert "[Uninstall Guide](docs/UNINSTALL.md)" in readme_text
    assert "[Release Checklist](RELEASING.md)" in readme_text


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
    checkout_cd_line = "cd /path/to/aira-agent-workspace-fabric"
    stop_guard_line = "if [ -f docker/compose/.env ]; then"
    stop_env_file_line = (
        "  docker compose --env-file docker/compose/.env -f docker/compose/local-service.yml stop"
    )
    stop_fallback_line = "  docker compose -f docker/compose/local-service.yml stop"
    stop_guard_end_line = "\nfi\n"
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
    intro_setup_line = (
        "uv run --python 3.12 --extra dev awf setup "
        "--source-checkout /path/to/replacement/aira-agent-workspace-fabric"
    )
    assert (
        "\nawf setup --source-checkout /path/to/replacement/aira-agent-workspace-fabric"
        not in intro_section
    )
    assert intro_words.index(core_stop_guidance) < intro_words.index(intro_setup_line)
    assert checkout_cd_line in intro_section
    assert stop_guard_line in intro_section
    assert stop_env_file_line in intro_section
    assert stop_fallback_line in intro_section
    assert stop_guard_end_line in intro_section
    intro_env_restore_start_index, intro_env_restore_end_index = (
        _assert_source_checkout_service_env_restore_before_stop(
            "intro source-checkout uninstall",
            intro_section,
            "refreshing source-checkout metadata",
        )
    )
    intro_fallback_index = intro_section.index(stop_fallback_line)
    assert (
        intro_section.index(checkout_cd_line)
        < intro_env_restore_start_index
        < intro_env_restore_end_index
        < intro_section.index(stop_guard_line)
        < intro_section.index(stop_env_file_line)
        < intro_fallback_index
        < _required_index(
            intro_section,
            stop_guard_end_line,
            "intro source-checkout uninstall",
            start=intro_fallback_index,
        )
        < intro_section.index(intro_setup_line)
    ), "intro must provide guarded Core stop commands before metadata refresh"
    for label, section, setup_line in source_cases:
        section_words = " ".join(section.split())
        assert core_stop_guidance in section_words, f"{label} must tell users to stop Core"
        assert port_block_guidance in section_words, f"{label} must explain setup port blockers"
        assert section_words.index(core_stop_guidance) < section_words.index(setup_line)
        assert checkout_cd_line in section, f"{label} must cd into the source checkout"
        assert stop_guard_line in section, f"{label} must provide a compose stop guard"
        assert stop_env_file_line in section, f"{label} must stop with compose env file"
        assert stop_fallback_line in section, f"{label} must stop without compose env file"
        assert stop_guard_end_line in section, f"{label} must close the compose stop guard"
        env_restore_start_index, env_restore_end_index = (
            _assert_source_checkout_service_env_restore_before_stop(
                f"{label} uninstall",
                section,
                "refreshing source-checkout metadata",
            )
        )
        stop_fallback_index = section.index(stop_fallback_line)
        assert (
            section.index(checkout_cd_line)
            < env_restore_start_index
            < env_restore_end_index
            < section.index(stop_guard_line)
            < section.index(stop_env_file_line)
            < stop_fallback_index
            < _required_index(
                section,
                stop_guard_end_line,
                f"{label} uninstall",
                start=stop_fallback_index,
            )
            < section.index(setup_line)
        ), f"{label} must provide guarded Core stop commands before metadata refresh"


def test_upgrade_no_global_source_checkout_rollback_uses_uv_run() -> None:
    """Assert no-global checkout rollback does not require a global awf executable."""
    upgrade_text = (REPO_ROOT / "docs" / "UPGRADE.md").read_text(encoding="utf-8")
    rollback_section = _markdown_section(upgrade_text, "## Rollback")
    no_global_heading = "For the source checkout with no global install lane"
    stop_guard_line = "if [ -f docker/compose/.env ]; then"
    stop_env_file_line = (
        "  docker compose --env-file docker/compose/.env -f docker/compose/local-service.yml stop"
    )
    stop_fallback_line = "  docker compose -f docker/compose/local-service.yml stop"
    stop_guard_end_line = "\nfi\n"
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
    assert stop_guard_line in no_global_section
    assert stop_env_file_line in no_global_section
    assert stop_fallback_line in no_global_section
    assert stop_guard_end_line in no_global_section
    env_restore_start_index, env_restore_end_index = (
        _assert_source_checkout_service_env_restore_before_stop(
            "no-global source-checkout rollback",
            no_global_section,
            "rollback",
        )
    )
    stop_fallback_index = no_global_section.index(stop_fallback_line)
    assert (
        no_global_section.index("uv sync --extra dev")
        < env_restore_start_index
        < env_restore_end_index
        < no_global_section.index(stop_guard_line)
        < no_global_section.index(stop_env_file_line)
        < stop_fallback_index
        < _required_index(
            no_global_section,
            stop_guard_end_line,
            "no-global source-checkout rollback",
            start=stop_fallback_index,
        )
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
    stop_guard_line = "if [ -f docker/compose/.env ]; then"
    stop_env_file_line = (
        "  docker compose --env-file docker/compose/.env -f docker/compose/local-service.yml stop"
    )
    stop_fallback_line = "  docker compose -f docker/compose/local-service.yml stop"
    stop_guard_end_line = "\nfi\n"
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
    assert stop_guard_line in global_section
    assert stop_env_file_line in global_section
    assert stop_fallback_line in global_section
    assert stop_guard_end_line in global_section
    env_restore_start_index, env_restore_end_index = (
        _assert_source_checkout_service_env_restore_before_stop(
            "global source-checkout rollback",
            global_section,
            "rollback",
        )
    )
    stop_fallback_index = global_section.index(stop_fallback_line)
    assert (
        global_section.index("uv tool install . --force")
        < env_restore_start_index
        < env_restore_end_index
        < global_section.index(stop_guard_line)
        < global_section.index(stop_env_file_line)
        < stop_fallback_index
        < _required_index(
            global_section,
            stop_guard_end_line,
            "global source-checkout rollback",
            start=stop_fallback_index,
        )
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

    module = sys.modules[__name__]
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

    module = sys.modules[__name__]
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

    module = sys.modules[__name__]
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

    module = sys.modules[__name__]
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(module, "README_PATH", tmp_path / "README.md")

    paths = [Path("README.md"), *map(Path, sorted(_public_docs()))]

    assert _public_docs() == {"docs/MISSING.md"}
    assert _awf_command_mentions(paths) == []


def _markdown_section(text: str, heading: str) -> str:
    """Return the body of the first matching H2 heading up to the next H2.

    Only H2 (``##``) headings are supported as the stop sentinel.
    """
    if not re.match(r"^##(?!#)(?:[ \t]|$)", heading):
        raise ValueError(f"Only H2 headings are supported: {heading!r}")

    normalized_text = text.replace("\r\n", "\n")
    heading_match = re.search(
        rf"(?m)^{re.escape(heading)}[ \t]*(?:\n|$)",
        normalized_text,
    )
    if heading_match is None:
        raise AssertionError(f"Markdown heading {heading!r} not found")

    start = heading_match.end()
    next_heading = re.search(r"(?m)^## ", normalized_text[start:])
    if next_heading is None:
        return normalized_text[start:]
    return normalized_text[start : start + next_heading.start()]


def _quickstart_upgrade_section(text: str, heading: str) -> str:
    """Return the lane's upgrade block between upgrade and uninstall labels."""
    section = _markdown_section(text, heading)
    upgrade_start = section.find("Upgrade:")
    assert upgrade_start != -1, f"{heading} is missing Upgrade block"
    uninstall_start = section.find("Uninstall:", upgrade_start)
    assert uninstall_start != -1, f"{heading} is missing Uninstall block after Upgrade"
    return section[upgrade_start + len("Upgrade:") : uninstall_start]


def _required_index(text: str, needle: str, label: str, start: int = 0) -> int:
    """Return a required substring index with assertion-style failure output."""
    index = text.find(needle, start)
    assert index != -1, f"{label} is missing required text after offset {start}: {needle!r}"
    return index


def _shell_closing_fi_index(section: str, start: int, label: str) -> int:
    """Return the closing fi index after a shell guard line."""
    closing_match = re.search(r"(?m)^fi$", section[start:])
    assert closing_match is not None, f"{label} is missing closing shell fi"
    return start + closing_match.start()


def _shell_line_index(section: str, line: str, label: str, start: int = 0) -> int:
    """Return the exact shell line index at or after start."""
    line_match = re.search(rf"(?m)^{re.escape(line)}$", section[start:])
    assert line_match is not None, f"{label} is missing shell line: {line}"
    return start + line_match.start()


def _assert_source_checkout_api_token_restore(
    label: str,
    section: str,
    lifecycle: str,
) -> tuple[int, int]:
    """Assert source-checkout snippets export persisted API tokens before use."""
    unsafe_default_line = 'export AWF_API_TOKEN="${AWF_API_TOKEN:-$(openssl rand -hex 32)}"'
    unsafe_shared_guard_line = (
        "if ! grep -q '^AWF_API_TOKEN=.' docker/compose/.env .env 2>/dev/null; then"
    )
    token_init_line = 'AWF_PERSISTED_API_TOKEN=""'
    token_loop_line = "for env_file in docker/compose/.env .env; do"
    token_file_guard_line = '  [ -f "$env_file" ] || continue'
    token_read_line = (
        '  AWF_PERSISTED_API_TOKEN="$(sed -n \'s/^AWF_API_TOKEN=//p\' "$env_file" | head -n 1)"'
    )
    token_break_line = '  [ -n "$AWF_PERSISTED_API_TOKEN" ] && break'
    token_loop_end_line = "done"
    token_guard_line = 'if [ -n "$AWF_PERSISTED_API_TOKEN" ]; then'
    token_persisted_export_line = '  export AWF_API_TOKEN="$AWF_PERSISTED_API_TOKEN"'
    token_else_line = "else"
    token_require_line = (
        '  : "${AWF_API_TOKEN:?restore the AWF_API_TOKEN used for the running local Core '
        "or persist it in docker/compose/.env or .env before " + lifecycle + '}"'
    )
    token_shell_export_line = "  export AWF_API_TOKEN"

    assert unsafe_default_line not in section, f"{label} must not regenerate AWF_API_TOKEN"
    assert unsafe_shared_guard_line not in section, (
        f"{label} must not let root .env satisfy the compose env guard without export"
    )
    assert token_init_line in section, f"{label} must initialize persisted API token lookup"
    assert token_loop_line in section, f"{label} must inspect source checkout env files"
    assert token_file_guard_line in section, f"{label} must skip absent env files"
    assert token_read_line in section, f"{label} must read persisted AWF_API_TOKEN"
    assert token_break_line in section, f"{label} must prefer the first persisted API token"
    assert token_guard_line in section, f"{label} must branch on persisted API token"
    assert token_persisted_export_line in section, (
        f"{label} must export the persisted AWF_API_TOKEN"
    )
    assert token_require_line in section, (
        f"{label} must require AWF_API_TOKEN when no persisted value exists"
    )
    assert token_shell_export_line in section, f"{label} must export restored shell AWF_API_TOKEN"

    token_init_index = section.index(token_init_line)
    token_loop_index = _required_index(section, token_loop_line, label, token_init_index)
    token_file_guard_index = _required_index(
        section,
        token_file_guard_line,
        label,
        token_loop_index,
    )
    token_read_index = _required_index(section, token_read_line, label, token_file_guard_index)
    token_break_index = _required_index(section, token_break_line, label, token_read_index)
    token_loop_end_index = _required_index(
        section,
        token_loop_end_line,
        label,
        token_break_index,
    )
    token_guard_index = _required_index(section, token_guard_line, label, token_loop_end_index)
    token_persisted_export_index = _required_index(
        section,
        token_persisted_export_line,
        label,
        start=token_guard_index,
    )
    token_else_index = _required_index(
        section,
        token_else_line,
        label,
        token_persisted_export_index,
    )
    token_require_index = _required_index(
        section,
        token_require_line,
        label,
        token_else_index,
    )
    token_shell_export_index = _required_index(
        section,
        token_shell_export_line,
        label,
        token_require_index,
    )
    token_guard_end_index = _shell_closing_fi_index(
        section,
        token_shell_export_index,
        label,
    )

    assert (
        token_init_index
        < token_loop_index
        < token_file_guard_index
        < token_read_index
        < token_break_index
        < token_loop_end_index
        < token_guard_index
        < token_persisted_export_index
        < token_else_index
        < token_require_index
        < token_shell_export_index
        < token_guard_end_index
    ), f"{label} must restore persisted API token before continuing"
    return token_init_index, token_guard_end_index


def _assert_source_checkout_postgres_password_restore(
    label: str,
    section: str,
    lifecycle: str,
) -> tuple[int, int]:
    """Assert source-checkout snippets preserve persisted Postgres passwords."""
    unsafe_default_line = 'export AWF_POSTGRES_PASSWORD="${AWF_POSTGRES_PASSWORD:-awf_dev}"'
    password_init_line = 'AWF_PERSISTED_POSTGRES_PASSWORD=""'
    password_loop_line = "for env_file in docker/compose/.env .env; do"
    password_file_guard_line = '  [ -f "$env_file" ] || continue'
    password_read_line = (
        '  AWF_PERSISTED_POSTGRES_PASSWORD="$(sed -n '
        "'s/^AWF_POSTGRES_PASSWORD=//p' "
        '"$env_file" | head -n 1)"'
    )
    password_break_line = '  [ -n "$AWF_PERSISTED_POSTGRES_PASSWORD" ] && break'
    password_loop_end_line = "done"
    password_guard_line = 'if [ -n "$AWF_PERSISTED_POSTGRES_PASSWORD" ]; then'
    password_persisted_export_line = (
        '  export AWF_POSTGRES_PASSWORD="$AWF_PERSISTED_POSTGRES_PASSWORD"'
    )
    password_else_line = "else"
    password_require_line = (
        '  : "${AWF_POSTGRES_PASSWORD:?restore the AWF_POSTGRES_PASSWORD used for '
        "the running local Core or persist it in docker/compose/.env or .env before "
        + lifecycle
        + '}"'
    )
    password_shell_export_line = "  export AWF_POSTGRES_PASSWORD"

    assert unsafe_default_line not in section, f"{label} must not default to awf_dev"
    assert password_init_line in section, f"{label} must initialize persisted password lookup"
    assert password_loop_line in section, f"{label} must inspect source checkout env files"
    assert password_file_guard_line in section, f"{label} must skip absent env files"
    assert password_read_line in section, f"{label} must read persisted AWF_POSTGRES_PASSWORD"
    assert password_break_line in section, f"{label} must prefer the first persisted password"
    assert password_guard_line in section, f"{label} must branch on persisted password"
    assert password_persisted_export_line in section, (
        f"{label} must export the persisted AWF_POSTGRES_PASSWORD"
    )
    assert password_require_line in section, (
        f"{label} must require AWF_POSTGRES_PASSWORD when no persisted value exists"
    )
    assert password_shell_export_line in section, (
        f"{label} must export restored shell AWF_POSTGRES_PASSWORD"
    )

    password_init_index = section.index(password_init_line)
    password_loop_index = _required_index(section, password_loop_line, label, password_init_index)
    password_file_guard_index = _required_index(
        section,
        password_file_guard_line,
        label,
        password_loop_index,
    )
    password_read_index = _required_index(
        section, password_read_line, label, password_file_guard_index
    )
    password_break_index = _required_index(section, password_break_line, label, password_read_index)
    password_loop_end_index = _required_index(
        section,
        password_loop_end_line,
        label,
        password_break_index,
    )
    password_guard_index = _required_index(
        section, password_guard_line, label, password_loop_end_index
    )
    password_persisted_export_index = _required_index(
        section,
        password_persisted_export_line,
        label,
        start=password_guard_index,
    )
    password_else_index = _required_index(
        section,
        password_else_line,
        label,
        password_persisted_export_index,
    )
    password_require_index = _required_index(
        section,
        password_require_line,
        label,
        password_else_index,
    )
    password_shell_export_index = _required_index(
        section,
        password_shell_export_line,
        label,
        password_require_index,
    )
    password_guard_end_index = _shell_closing_fi_index(
        section,
        password_shell_export_index,
        label,
    )

    assert (
        password_init_index
        < password_loop_index
        < password_file_guard_index
        < password_read_index
        < password_break_index
        < password_loop_end_index
        < password_guard_index
        < password_persisted_export_index
        < password_else_index
        < password_require_index
        < password_shell_export_index
        < password_guard_end_index
    ), f"{label} must restore persisted Postgres password before continuing"
    return password_init_index, password_guard_end_index


def _assert_source_checkout_service_env_restore_before_stop(
    label: str,
    section: str,
    lifecycle: str,
) -> tuple[int, int]:
    """Assert source-checkout snippets restore service secrets before stopping Core."""
    stop_guard_line = "if [ -f docker/compose/.env ]; then"

    api_restore_start_index, api_restore_end_index = _assert_source_checkout_api_token_restore(
        label,
        section,
        lifecycle,
    )
    password_restore_start_index, password_restore_end_index = (
        _assert_source_checkout_postgres_password_restore(
            label,
            section,
            lifecycle,
        )
    )
    stop_guard_index = _shell_line_index(
        section, stop_guard_line, label, password_restore_end_index
    )

    assert (
        api_restore_start_index
        < api_restore_end_index
        < password_restore_start_index
        < password_restore_end_index
        < stop_guard_index
    ), f"{label} must restore service secrets before stopping Core"
    return api_restore_start_index, password_restore_end_index


def _assert_package_upgrade_restores_service_env(
    label: str,
    section: str,
    upgrade_line: str,
) -> None:
    """Assert package upgrade snippets restore service environment before restart."""
    api_guard_line = "if ! grep -q '^AWF_API_TOKEN=.' .env 2>/dev/null; then"
    api_require_line = (
        '  : "${AWF_API_TOKEN:?restore the AWF_API_TOKEN used for the running local Core '
        'or persist it in .env before upgrading}"'
    )
    api_export_line = "  export AWF_API_TOKEN"
    unsafe_api_generation_line = (
        '  export AWF_API_TOKEN="${AWF_API_TOKEN:-$(openssl rand -hex 32)}"'
    )
    password_guard_line = "if ! grep -q '^AWF_POSTGRES_PASSWORD=.' .env 2>/dev/null; then"
    password_require_line = (
        '  : "${AWF_POSTGRES_PASSWORD:?restore the AWF_POSTGRES_PASSWORD used for '
        'the running local Core or persist it in .env before upgrading}"'
    )
    password_export_line = "  export AWF_POSTGRES_PASSWORD"
    unsafe_password_default_line = (
        'export AWF_POSTGRES_PASSWORD="${AWF_POSTGRES_PASSWORD:-awf_dev}"'
    )
    start_line = "\nawf start\n"

    assert upgrade_line in section, f"{label} is missing upgrade command"
    assert api_guard_line in section, f"{label} must prefer persisted AWF_API_TOKEN"
    assert api_require_line in section, f"{label} must require the existing AWF_API_TOKEN"
    assert api_export_line in section, f"{label} must export restored AWF_API_TOKEN"
    assert unsafe_api_generation_line not in section, f"{label} must not regenerate AWF_API_TOKEN"
    assert password_guard_line in section, f"{label} must prefer persisted AWF_POSTGRES_PASSWORD"
    assert unsafe_password_default_line not in section, (
        f"{label} must not default AWF_POSTGRES_PASSWORD"
    )
    assert password_require_line in section, (
        f"{label} must require the existing AWF_POSTGRES_PASSWORD"
    )
    assert password_export_line in section, f"{label} must export restored AWF_POSTGRES_PASSWORD"
    assert start_line in section, f"{label} is missing restart command"

    upgrade_index = _required_index(section, upgrade_line, label)
    api_guard_index = _shell_line_index(section, api_guard_line, label, upgrade_index)
    api_require_index = _shell_line_index(section, api_require_line, label, api_guard_index)
    api_export_index = _shell_line_index(section, api_export_line, label, api_require_index)
    api_guard_end_index = _shell_closing_fi_index(section, api_export_index, label)
    password_guard_index = _shell_line_index(
        section,
        password_guard_line,
        label,
        api_export_index,
    )
    password_require_index = _shell_line_index(
        section,
        password_require_line,
        label,
        password_guard_index,
    )
    password_export_index = _shell_line_index(
        section,
        password_export_line,
        label,
        password_require_index,
    )
    password_guard_end_index = _shell_closing_fi_index(section, password_export_index, label)
    start_index = _required_index(section, start_line, label, start=password_guard_end_index)
    assert (
        upgrade_index
        < api_guard_index
        < api_require_index
        < api_export_index
        < api_guard_end_index
        < password_guard_index
        < password_require_index
        < password_export_index
        < password_guard_end_index
        < start_index
    ), f"{label} must restore missing service env before restart"


def _public_docs() -> set[str]:
    public_docs = _all_public_markdown_docs()
    public_docs.update(_readme_public_doc_links())
    public_docs.update(_present_docs(OPTIONAL_PUBLIC_GUIDES))
    return {doc for doc in public_docs if _is_public_doc_path(doc)}


def _all_public_markdown_docs() -> set[str]:
    docs_dir = REPO_ROOT / "docs"
    if not docs_dir.exists():
        return set()

    return {
        path.relative_to(REPO_ROOT).as_posix()
        for path in docs_dir.rglob("*.md")
        if _is_public_doc_path(path.relative_to(REPO_ROOT).as_posix())
    }


def _docs_index_links() -> set[str]:
    links: set[str] = set()
    for index_path in DOCS_INDEX_CANDIDATES:
        if index_path.exists():
            links.update(_markdown_doc_links(index_path))
    return {link for link in links if _is_public_doc_path(link)}


def _readme_public_doc_links() -> set[str]:
    return {link for link in _markdown_doc_links(README_PATH) if _is_public_doc_path(link)}


def _markdown_doc_links(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    return {
        resolved
        for target in LINK_RE.findall(text)
        if (resolved := _resolve_markdown_link(path, target)) is not None
    }


def _resolve_markdown_link(source_path: Path, target: str) -> str | None:
    target = target.strip()
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc:
        return None
    raw_path = unquote(parsed.path)
    if not raw_path or raw_path.startswith("#") or not raw_path.endswith(".md"):
        return None

    base_dir = source_path.parent
    resolved = (base_dir / raw_path).resolve()
    try:
        rel_path = resolved.relative_to(REPO_ROOT)
    except ValueError:
        return None
    return rel_path.as_posix()


def _is_public_doc_path(rel_path: str) -> bool:
    if rel_path in ROOT_PUBLIC_DOC_NAMES:
        return True
    if not rel_path.startswith("docs/"):
        return False
    if any(rel_path.startswith(prefix) for prefix in INTERNAL_DOC_PREFIXES):
        return False
    return rel_path not in PLANNING_DOC_NAMES


def _present_docs(candidates: Iterable[str]) -> set[str]:
    return {rel_path for rel_path in candidates if (REPO_ROOT / rel_path).exists()}


def _typer_command_tree(typer_app: object) -> set[tuple[str, ...]]:
    command_paths: set[tuple[str, ...]] = set()
    for command in typer_app.registered_commands:
        command_paths.add((_command_name(command),))
    for group in typer_app.registered_groups:
        group_name = group.name
        for command in group.typer_instance.registered_commands:
            command_paths.add((group_name, _command_name(command)))
    return command_paths


def _command_name(command: object) -> str:
    explicit_name = command.name
    if explicit_name:
        return explicit_name
    return command.callback.__name__.replace("_", "-")


def _awf_command_mentions(paths: Iterable[Path]) -> list[AwfCommandMention]:
    root_groups = {path[0] for path in _typer_command_tree(app) if len(path) == 2}
    mentions: list[AwfCommandMention] = []
    for rel_path in paths:
        doc_path = REPO_ROOT / rel_path
        if not doc_path.exists():
            continue
        text = doc_path.read_text(encoding="utf-8")
        collapsed = re.sub(r"\\\n\s*", " ", text)
        for line in collapsed.splitlines():
            if _ignore_awf_command_line(line):
                continue
            for match in AWF_COMMAND_RE.finditer(line):
                raw_command = f"awf{match.group('tail')}".strip()
                command_path = _awf_command_path(raw_command, root_groups)
                if command_path is None:
                    continue
                mentions.append(
                    AwfCommandMention(
                        path=rel_path.as_posix(),
                        command=raw_command,
                        command_path=command_path,
                    )
                )
    return mentions


def _ignore_awf_command_line(line: str) -> bool:
    lowered = line.lower()
    return "currently not implemented" in lowered or "future" in lowered


def _awf_command_path(
    raw_command: str,
    root_groups: set[str],
) -> tuple[str, ...] | None:
    try:
        tokens = shlex.split(raw_command, comments=True)
    except ValueError:
        tokens = raw_command.split()
    tokens = [_clean_token(token) for token in tokens]
    if not tokens or tokens[0] != "awf":
        return None
    if len(tokens) == 1:
        return None

    first = tokens[1]
    if not _looks_like_command_token(first):
        return None
    if first not in root_groups:
        return (first,)
    if len(tokens) < 3:
        return None

    second = tokens[2]
    if not _looks_like_command_token(second):
        return None
    return (first, second)


def _clean_token(token: str) -> str:
    return token.strip("`'\".,: ")


def _looks_like_command_token(token: str) -> bool:
    if not token or token.startswith("-"):
        return False
    if token.startswith(("<", "{", "$")):
        return False
    if token in {".", "...", "&&", "||", "|", ";"}:
        return False
    return not ("/" in token or "=" in token)


def _copy_paste_docs() -> set[str]:
    docs = _present_docs(COPY_PASTE_DOC_HINTS)
    docs.update(
        rel_path
        for rel_path in _present_docs(_public_docs())
        if "copy-paste" in (REPO_ROOT / rel_path).read_text(encoding="utf-8").lower()
        and rel_path.endswith(".md")
    )
    return docs


def _fence_delimiter_count_is_even(text: str) -> bool:
    return len(FENCE_DELIMITER_RE.findall(text)) % 2 == 0


def _markdown_fences(rel_path: str, text: str) -> list[MarkdownFence]:
    fences: list[MarkdownFence] = []
    lines = text.splitlines()
    line_index = 0

    while line_index < len(lines):
        opening_match = OPENING_FENCE_RE.match(lines[line_index])
        if opening_match is None:
            line_index += 1
            continue

        opening_line = line_index + 1
        indent_width = len(opening_match.group("indent"))
        delimiter_width = len(opening_match.group("delimiter"))
        body_lines: list[str] = []
        line_index += 1

        while line_index < len(lines):
            line = lines[line_index]
            if _is_closing_fence(line, delimiter_width):
                fences.append(
                    MarkdownFence(
                        path=rel_path,
                        line=opening_line,
                        language=opening_match.group("language").lower(),
                        body="\n".join(body_lines).strip("\n"),
                    )
                )
                break

            body_lines.append(_strip_fence_body_indent(line, indent_width))
            line_index += 1
        line_index += 1
    return fences


def _is_closing_fence(line: str, delimiter_width: int) -> bool:
    match = re.match(r"^ {0,3}(?P<delimiter>`{3,})[ \t]*$", line)
    return match is not None and len(match.group("delimiter")) >= delimiter_width


def _strip_fence_body_indent(line: str, indent_width: int) -> str:
    leading_spaces = len(line) - len(line.lstrip(" "))
    return line[min(indent_width, leading_spaces) :]


def _assert_snippet_syntax(fence: MarkdownFence) -> None:
    if not fence.body.strip():
        pytest.fail(f"{fence.path}:{fence.line} has an empty copy-paste snippet")

    language = fence.language
    if language == "json":
        json.loads(fence.body)
    elif language in {"yaml", "yml"}:
        yaml.safe_load(fence.body)
    elif language == "python":
        ast.parse(fence.body)
    elif language in {"bash", "sh", "shell"}:
        script = _strip_shell_prompts(fence.body)
        try:
            result = subprocess.run(
                ["bash", "-n"],
                input=script,
                text=True,
                capture_output=True,
                check=False,
                timeout=5,
            )
        except FileNotFoundError:
            pytest.fail(
                f"{fence.path}:{fence.line} cannot validate shell snippet because "
                "bash is not available on PATH"
            )
        assert result.returncode == 0, (
            f"{fence.path}:{fence.line} has invalid shell syntax: {result.stderr.strip()}"
        )
    else:
        assert fence.body.strip(), f"{fence.path}:{fence.line} has empty text snippet"


def _strip_shell_prompts(script: str) -> str:
    stripped_lines: list[str] = []
    for line in script.splitlines():
        if line.startswith(("$ ", "> ")):
            stripped_lines.append(line[2:])
        else:
            stripped_lines.append(line)
    return "\n".join(stripped_lines)
