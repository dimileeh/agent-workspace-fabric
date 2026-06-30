"""Tests for declarative Playwright browser-install command planning.

The minimal design: a repo declares ``runtime.browsers`` and AWF emits a single
``playwright install <browsers>`` command for the setup phase, choosing the Node
package manager or the Python interpreter from the profile's existing commands.
"""

from __future__ import annotations

import pytest

from awf.profiles.models import WorkspaceProfile
from awf.runtime.node_playwright_setup import (
    playwright_browser_install_command,
    playwright_command,
)
from awf.runtime.validation_setup import profile_phase_command_plan

_BROWSER_INSTALL_TIMEOUT = 900


def _profile(payload: dict[str, object]) -> WorkspaceProfile:
    return WorkspaceProfile.model_validate({"name": "pw-profile", **payload})


@pytest.mark.unit
@pytest.mark.parametrize(
    ("package_manager", "expected"),
    [
        ("pnpm", "pnpm exec playwright install chromium"),
        ("yarn", "yarn playwright install chromium"),
        ("bun", "bunx playwright install chromium"),
        ("npm", "npx playwright install chromium"),
        ("/usr/local/bin/pnpm", "pnpm exec playwright install chromium"),
    ],
)
def test_playwright_command_is_package_manager_aware(package_manager: str, expected: str) -> None:
    # A bare path-qualified manager still resolves by its leading token.
    assert playwright_command(package_manager.rsplit("/", 1)[-1], "install", "chromium") == expected


@pytest.mark.unit
def test_playwright_command_handles_unparseable_and_empty_manager() -> None:
    # An unbalanced quote makes shlex raise; we fall back to the raw token (npx).
    assert playwright_command('"', "test") == "npx playwright test"
    # An empty manager string yields no tokens and defaults to npx.
    assert playwright_command("", "test") == "npx playwright test"


@pytest.mark.unit
def test_browser_install_detects_node_manager_from_bare_install_token() -> None:
    # A bare manager token (no subcommand) still counts as an install signal.
    profile = _profile({"runtime": {"browsers": ["chromium"]}, "phases": {"setup": ["yarn"]}})

    command = playwright_browser_install_command(profile)

    assert command is not None
    assert command.command == "yarn playwright install chromium"


@pytest.mark.unit
def test_browser_install_ignores_node_manager_with_non_install_subcommand() -> None:
    # ``pnpm run build`` is not an install signal, so detection falls through to npx.
    profile = _profile(
        {"runtime": {"browsers": ["chromium"]}, "phases": {"setup": ["pnpm run build"]}}
    )

    command = playwright_browser_install_command(profile)

    assert command is not None
    assert command.command == "npx playwright install chromium"


@pytest.mark.unit
def test_browser_install_skips_unparseable_commands_and_still_finds_python() -> None:
    # The malformed (unbalanced-quote) command is skipped by both the node-manager
    # scan and the python-interpreter scan; detection continues to ``uv run``.
    profile = _profile(
        {
            "runtime": {"browsers": ["chromium"]},
            "phases": {"setup": ['echo "unterminated'], "validate": ["uv run pytest"]},
        }
    )

    command = playwright_browser_install_command(profile)

    assert command is not None
    assert command.command == "uv run python -m playwright install chromium"


@pytest.mark.unit
def test_browser_install_is_none_without_declared_browsers() -> None:
    assert playwright_browser_install_command(_profile({"phases": {"setup": ["npm ci"]}})) is None


@pytest.mark.unit
def test_browser_install_prefers_detected_node_package_manager() -> None:
    profile = _profile(
        {"runtime": {"browsers": ["chromium", "firefox"]}, "phases": {"setup": ["pnpm install"]}}
    )

    command = playwright_browser_install_command(profile)

    assert command is not None
    assert command.command == "pnpm exec playwright install chromium firefox"
    assert command.timeout_seconds == _BROWSER_INSTALL_TIMEOUT
    assert command.required is False


@pytest.mark.unit
@pytest.mark.parametrize(
    ("setup_command", "expected_install_command"),
    [
        (
            "npm --prefix apps/console ci",
            "npm --prefix apps/console exec playwright install chromium",
        ),
        (
            "pnpm --dir apps/console install",
            "pnpm --dir apps/console exec playwright install chromium",
        ),
        (
            "npm ci --prefix=apps/console",
            "npm --prefix=apps/console exec playwright install chromium",
        ),
        (
            "pnpm install --dir apps/console",
            "pnpm --dir apps/console exec playwright install chromium",
        ),
    ],
)
def test_browser_install_preserves_package_manager_scope(
    setup_command: str, expected_install_command: str
) -> None:
    profile = _profile(
        {"runtime": {"browsers": ["chromium"]}, "phases": {"setup": [setup_command]}}
    )

    command = playwright_browser_install_command(profile)

    assert command is not None
    assert command.command == expected_install_command


@pytest.mark.unit
@pytest.mark.parametrize(
    ("validate_command", "expected_executable_prefix"),
    [
        (
            "uv run --project apps/api pytest",
            "uv run --project apps/api python",
        ),
        (
            "uv run --directory apps/api pytest",
            "uv run --directory apps/api python",
        ),
        (
            "uv run --python 3.12 --extra dev pytest",
            "uv run --python 3.12 --extra dev python",
        ),
        (
            "uv run --project=apps/api pytest",
            "uv run --project=apps/api python",
        ),
    ],
)
def test_browser_install_preserves_uv_run_project_scope(
    validate_command: str, expected_executable_prefix: str
) -> None:
    profile = _profile(
        {"runtime": {"browsers": ["chromium"]}, "phases": {"validate": [validate_command]}}
    )

    command = playwright_browser_install_command(profile)

    assert command is not None
    assert command.command == f"{expected_executable_prefix} -m playwright install chromium"


@pytest.mark.unit
def test_browser_install_uses_python_interpreter_when_no_node_manager() -> None:
    profile = _profile(
        {
            "runtime": {"browsers": ["chromium"]},
            "phases": {"setup": ["pip install -r requirements.txt"], "validate": ["uv run pytest"]},
        }
    )

    command = playwright_browser_install_command(profile)

    assert command is not None
    assert command.command == "uv run python -m playwright install chromium"


@pytest.mark.unit
def test_browser_install_recognizes_bare_python_and_pytest_playwright() -> None:
    explicit_python = _profile(
        {"runtime": {"browsers": ["webkit"]}, "phases": {"validate": ["python3 -m pytest"]}}
    )
    assert playwright_browser_install_command(explicit_python) is not None
    assert (
        playwright_browser_install_command(explicit_python).command
        == "python3 -m playwright install webkit"
    )

    pytest_playwright = _profile(
        {"runtime": {"browsers": ["chromium"]}, "phases": {"validate": ["pytest tests/playwright"]}}
    )
    assert (
        playwright_browser_install_command(pytest_playwright).command
        == "python -m playwright install chromium"
    )


@pytest.mark.unit
def test_browser_install_falls_back_to_npx() -> None:
    profile = _profile({"runtime": {"browsers": ["chromium"]}, "phases": {"validate": ["echo hi"]}})

    command = playwright_browser_install_command(profile)

    assert command is not None
    assert command.command == "npx playwright install chromium"


@pytest.mark.unit
@pytest.mark.parametrize(
    "validate_command",
    ["npx playwright test", "playwright test"],
)
def test_browser_install_does_not_treat_node_playwright_cli_as_python(
    validate_command: str,
) -> None:
    profile = _profile(
        {"runtime": {"browsers": ["chromium"]}, "phases": {"validate": [validate_command]}}
    )

    command = playwright_browser_install_command(profile)

    assert command is not None
    assert command.command == "npx playwright install chromium"


@pytest.mark.unit
def test_setup_plan_appends_browser_install_after_setup_and_db_hooks() -> None:
    profile = _profile(
        {
            "runtime": {"browsers": ["chromium"]},
            "phases": {"setup": ["npm ci"]},
            "database": {"generated_setup": ["psql -c 'select 1'"]},
        }
    )

    plan = profile_phase_command_plan(profile, ["setup"])
    commands = [step.command.command for step in plan]

    assert commands == ["npm ci", "psql -c 'select 1'", "npx playwright install chromium"]
    assert plan[-1].phase == "setup"


@pytest.mark.unit
def test_setup_plan_omits_browser_install_when_no_browsers_declared() -> None:
    profile = _profile({"phases": {"setup": ["npm ci"]}})

    plan = profile_phase_command_plan(profile, ["setup"])

    assert all("playwright install" not in step.command.command for step in plan)
