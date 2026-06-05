from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path
from urllib.parse import quote

import pytest

from tests.unit.docs.public_docs_status_helpers import (
    PACKAGE_DATABASE_URL_ENCODED_EXPORT,
    PACKAGE_DATABASE_URL_RAW_PASSWORD_EXPORT,
    PACKAGE_POSTGRES_PASSWORD_URLENCODE_LINE,
    README_PATH,
    REPO_ROOT,
    _assert_snippet_syntax,
    _assert_source_checkout_service_env_restore_before_stop,
    _awf_command_mentions,
    _copy_paste_docs,
    _docs_index_links,
    _fence_delimiter_count_is_even,
    _markdown_fences,
    _markdown_section,
    _public_docs,
    _required_index,
    _typer_command_tree,
    app,
)


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
    api_persist = "  printf 'AWF_API_TOKEN=%s\\n' \"$AWF_API_TOKEN\""
    password_dotenv_export = 'awf_postgres_password_dotenv="$('
    password_persist = "  printf 'AWF_POSTGRES_PASSWORD=%s\\n' \"$awf_postgres_password_dotenv\""
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
    assert PACKAGE_POSTGRES_PASSWORD_URLENCODE_LINE in first_run_section
    assert password_dotenv_export in first_run_section
    assert "AWF_POSTGRES_PASSWORD cannot contain newlines" in first_run_section
    assert PACKAGE_DATABASE_URL_ENCODED_EXPORT in first_run_section
    assert PACKAGE_DATABASE_URL_RAW_PASSWORD_EXPORT not in first_run_section
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
    ordered_steps = (
        ("API token export", api_export),
        ("Postgres password export", password_export),
        ("Postgres host port export", host_port_export),
        ("Postgres password URL encoding", PACKAGE_POSTGRES_PASSWORD_URLENCODE_LINE),
        ("Postgres password dotenv escaping", password_dotenv_export),
        ("database URL export", PACKAGE_DATABASE_URL_ENCODED_EXPORT),
        ("temporary env file creation", env_tmp),
        ("API token persist", api_persist),
        ("Postgres password persist", password_persist),
        ("Postgres host port persist", host_port_persist),
        ("database URL persist", database_url_persist),
        ("existing env preservation", preserve_existing_env),
        ("env file replacement", persist_target),
        ("setup command", setup_command),
    )
    for (previous_label, previous_text), (next_label, next_text) in zip(
        ordered_steps,
        ordered_steps[1:],
        strict=False,
    ):
        previous_index = _required_index(first_run_section, previous_text, previous_label)
        next_index = _required_index(first_run_section, next_text, next_label)
        assert previous_index < next_index, (
            "Quickstart package first-run service env steps are out of order: "
            f"{previous_label} at {previous_index} must precede {next_label} at {next_index}"
        )


def test_quickstart_package_first_run_url_encodes_custom_postgres_password(
    tmp_path: Path,
) -> None:
    """Assert custom Postgres passwords are safe for URL and dotenv parsing."""
    from awf.service.environment import compose_env_file_values

    quickstart_text = (REPO_ROOT / "docs" / "QUICKSTART.md").read_text(encoding="utf-8")
    lane_section = _markdown_section(quickstart_text, "## Lane 1: uv tool or pipx")
    first_run_section = lane_section.split("\nUpgrade:\n", maxsplit=1)[0]
    bash_fences = [
        fence
        for fence in _markdown_fences("docs/QUICKSTART.md", first_run_section)
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
    ("heading", "setup_command", "status_command"),
    (
        (
            "## Lane 2: Source Checkout With Global Tool Install",
            'awf setup --source-checkout "$PWD"',
            "awf service status --format pretty",
        ),
        (
            "## Lane 3: Source Checkout With No Global Install",
            'uv run --python 3.12 --extra dev awf setup --source-checkout "$PWD"',
            "uv run --python 3.12 --extra dev awf service status --format pretty",
        ),
    ),
)
def test_quickstart_source_checkout_first_run_uses_root_env(
    heading: str,
    setup_command: str,
    status_command: str,
) -> None:
    """Assert source-checkout first-run commands use root `.env` before startup."""
    quickstart_text = (REPO_ROOT / "docs" / "QUICKSTART.md").read_text(encoding="utf-8")
    lane_section = _markdown_section(quickstart_text, heading)
    first_run_section = lane_section.split("\nUpgrade:\n", maxsplit=1)[0]
    assert "Keep local runtime values in the checkout-root `.env`" in first_run_section
    assert "cp .env.example .env" in first_run_section
    assert "docker/compose/.env" not in first_run_section
    assert 'awf_env_tmp="$(mktemp)"' not in first_run_section
    assert "awf service bootstrap" not in first_run_section
    assert f"\n{setup_command}\n" in first_run_section
    assert status_command in first_run_section
    assert first_run_section.index("cp .env.example .env") < first_run_section.index(
        f"\n{setup_command}\n",
    )
    assert first_run_section.index(f"\n{setup_command}\n") < first_run_section.index(
        status_command,
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
    assert len(lane_headings) >= 3
    for heading in lane_headings:
        section = _markdown_section(quickstart_text, heading)
        first_run_section = section.split("\nUpgrade:\n", maxsplit=1)[0]
        optional_token_comment_count = first_run_section.count(optional_token_comment)
        manual_token_comment_count = first_run_section.count(manual_token_comment)
        assert optional_token_comment_count == 1, (
            f"{heading} must include exactly one optional GitHub token scope comment "
            "before Upgrade; "
            f"found {optional_token_comment_count}"
        )
        assert manual_token_comment_count == 1, (
            f"{heading} must include exactly one manual GitHub token guidance comment "
            "before Upgrade; "
            f"found {manual_token_comment_count}"
        )


def test_quickstart_clears_source_checkout_metadata_before_checkout_deletion() -> None:
    """Assert Quickstart does not leave source-checkout metadata stale."""
    quickstart_text = (REPO_ROOT / "docs" / "QUICKSTART.md").read_text(encoding="utf-8")
    core_stop_guidance = "Stop local Core before refreshing source-checkout metadata"
    port_block_guidance = (
        "`awf setup` checks the API and Postgres host ports and blocks while the previous "
        "Core stack still holds them"
    )
    no_stop_guidance = "Editing `~/.awf/config.yml` remains the no-stop option"
    stop_line = "docker compose --env-file .env -f docker/compose/local-service.yml stop"
    source_lane_headings = (
        "## Lane 2: Source Checkout With Global Tool Install",
        "## Lane 3: Source Checkout With No Global Install",
    )

    for heading in source_lane_headings:
        source_section = _markdown_section(quickstart_text, heading)
        uninstall_label = "\nUninstall:\n"
        uninstall_start = source_section.find(uninstall_label)
        assert uninstall_start != -1, f"{heading} is missing standalone Uninstall label"
        uninstall_section = source_section[uninstall_start + len(uninstall_label) :]
        section_words = " ".join(uninstall_section.split())
        if heading == "## Lane 3: Source Checkout With No Global Install":
            replacement_setup = (
                "uv run --python 3.12 --extra dev awf setup "
                "--source-checkout /path/to/replacement/aira-agent-workspace-fabric"
            )
        else:
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
                require_legacy_fallback=True,
            )
        )
        assert stop_line in uninstall_section
        assert "rm -rf aira-agent-workspace-fabric" in uninstall_section
        stop_index = uninstall_section.index(stop_line)
        assert section_words.index(core_stop_guidance) < section_words.index(port_block_guidance)
        assert (
            env_restore_start_index
            < env_restore_end_index
            < stop_index
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
