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
    assert "For lanes that put `awf` on `PATH`" in first_run_section
    assert "For the no-global source checkout lane" in first_run_section
    assert "awf init <path>" in first_run_section
    assert "awf smoke run --project <path> --mocked-local --format pretty" in first_run_section
    assert "awf smoke run --mocked-local --format pretty" not in first_run_section
    assert "uv run --python 3.12 --extra dev awf setup" in first_run_section
    assert "uv run --python 3.12 --extra dev awf start" in first_run_section
    assert "uv run --python 3.12 --extra dev awf init <path>" in first_run_section
    assert (
        "uv run --python 3.12 --extra dev awf smoke run --project <path> "
        "--mocked-local --format pretty"
    ) in first_run_section


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
    source_lane_headings = (
        "## Lane 2: Source Checkout With Global Tool Install",
        "## Lane 3: Source Checkout With No Global Install",
    )

    for heading in source_lane_headings:
        source_section = _markdown_section(quickstart_text, heading)
        replacement_setup = (
            "awf setup --source-checkout /path/to/replacement/aira-agent-workspace-fabric"
        )
        assert "~/.awf/config.yml" in source_section
        assert "remove only the top-level `source_checkout:` block" in source_section
        assert replacement_setup in source_section
        assert source_section.index("~/.awf/config.yml") < source_section.index(
            "rm -rf aira-agent-workspace-fabric"
        )
        if heading == "## Lane 2: Source Checkout With Global Tool Install":
            assert source_section.index(replacement_setup) < source_section.index(
                "uv tool uninstall agent-workspace-fabric"
            )


def test_source_checkout_upgrade_docs_refresh_persisted_metadata() -> None:
    """Assert source-checkout upgrades refresh persisted asset metadata."""
    quickstart_text = (REPO_ROOT / "docs" / "QUICKSTART.md").read_text(encoding="utf-8")
    upgrade_text = (REPO_ROOT / "docs" / "UPGRADE.md").read_text(encoding="utf-8")
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
        assert setup_line in section, f"{label} does not refresh source_checkout metadata"
        assert start_line in section, f"{label} is missing source-checkout start"
        assert (
            section.index(refresh_prereq) < section.index(setup_line) < section.index(start_line)
        ), f"{label} must refresh metadata after upgrade and before start"


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
    api_url = DEFAULT_LOCAL_SERVICE_API_BASE_URL
    readyz_url = f"{api_url.rstrip('/')}/readyz"
    console_url = DEFAULT_LOCAL_CONSOLE_URL

    assert f"`{api_url}` by default" in quickstart_text
    assert f"`{console_url}` when the console is running" in quickstart_text
    assert f"`{console_url}` for the console" in quickstart_text
    assert f"`{readyz_url}`" in quickstart_text
    assert "http://127.0.0.1:8000" not in quickstart_text
    assert "http://127.0.0.1:3000" not in quickstart_text
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
    )[1].split("### Run Locally", maxsplit=1)[0]

    assert "awf setup" in startup_section
    assert "awf start" in startup_section
    assert "awf init <path> --write-profile --yes" in startup_section
    assert "awf smoke run --project <path> --mocked-local --format pretty" in startup_section
    assert "AWF_SETUP_PLACEHOLDER" not in startup_section
    assert "AWF_START_PLACEHOLDER" not in startup_section
    assert not re.search(r"(?m)^awf service bootstrap\s*$", startup_section)
    assert re.search(r"`awf start`\s+uses", configure_section)
    assert "`awf service bootstrap` uses" not in configure_section
    assert "run `awf start`" in configure_section


def test_getting_started_cli_host_port_derivation_matches_cli_default() -> None:
    """Assert Getting Started documents the CLI's localhost host-port derivation."""
    getting_started_text = (REPO_ROOT / "docs" / "GETTING_STARTED.md").read_text(encoding="utf-8")
    configure_section = getting_started_text.split(
        "### Configure Environment",
        maxsplit=1,
    )[1].split("### Run Locally", maxsplit=1)[0]

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

    assert len(re.findall(r"(?m)^awf setup\s*$", prerequisites_section)) == 2
    assert len(re.findall(r"(?m)^awf start\s*$", prerequisites_section)) == 2
    assert re.search(r"(?m)^} > \.env\s*\nawf setup\nawf start$", prerequisites_section)
    assert re.search(
        r"(?m)^} > docker/compose/\.env\s*\nawf setup\nawf start$",
        prerequisites_section,
    )
    assert (
        'export AWF_DATABASE_URL="postgresql+asyncpg://awf:'
        '${AWF_POSTGRES_PASSWORD}@127.0.0.1:${AWF_POSTGRES_HOST_PORT}/awf"'
    ) in prerequisites_section
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
    assert "Upgrade:" in section, f"{heading} is missing Upgrade block"
    assert "Uninstall:" in section, f"{heading} is missing Uninstall block"
    return section.split("Upgrade:", maxsplit=1)[1].split("Uninstall:", maxsplit=1)[0]


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
