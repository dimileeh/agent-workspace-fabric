"""Tests for declarative Playwright browser-install command planning.

The minimal design: a repo declares ``runtime.browsers`` and AWF emits a single
``playwright install <browsers>`` command for the setup phase, choosing the Node
package manager or the Python interpreter from the profile's existing commands.
"""

from __future__ import annotations

import shlex

import pytest

from awf.profiles.models import WorkspaceProfile
from awf.runtime.node_playwright_setup import (
    _cd_prefix_from_cd_only_segment,
    _collect_pm_scope_tokens,
    _collect_uv_global_scope_tokens,
    _collect_uv_pip_scope_tokens,
    _collect_uv_run_scope_tokens,
    _collect_uv_sync_scope_tokens,
    _detected_node_package_manager,
    _extract_cd_scope_prefix,
    _has_pip_install_subcommand,
    _is_node_option_only_install_command,
    _pip_to_python_executable,
    _pm_invocation_tokens,
    _python_executable_from_commands,
    _python_playwright_executable,
    _uv_pip_system_python_executable,
    _uv_run_python_prefix,
    _uv_setup_python_prefix,
    playwright_browser_install_command,
    playwright_command,
)
from awf.runtime.validation_setup import profile_phase_command_plan

_BROWSER_INSTALL_TIMEOUT = 900


def _profile(payload: dict[str, object]) -> WorkspaceProfile:
    """Test helper for profile."""
    return WorkspaceProfile.model_validate({"name": "pw-profile", **payload})


@pytest.mark.unit
@pytest.mark.parametrize(
    ("package_manager", "expected"),
    [
        ("pnpm", "pnpm exec playwright install chromium"),
        ("yarn", "yarn playwright install chromium"),
        ("bun", "bunx playwright install chromium"),
        ("npm", "npx playwright install chromium"),
    ],
)
def test_playwright_command_is_package_manager_aware(package_manager: str, expected: str) -> None:
    """Verify playwright command is package manager aware."""
    assert playwright_command(package_manager, "install", "chromium") == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("package_manager", "expected"),
    [
        ("/opt/pnpm/bin/pnpm", "/opt/pnpm/bin/pnpm exec playwright install chromium"),
        (
            "/repo/.yarn/releases/yarn",
            "/repo/.yarn/releases/yarn playwright install chromium",
        ),
        ("/opt/bun/bin/bun", "/opt/bun/bin/bun x playwright install chromium"),
    ],
)
def test_playwright_command_preserves_path_qualified_manager(
    package_manager: str, expected: str
) -> None:
    """Verify path-qualified package managers keep their executable prefix."""
    assert playwright_command(package_manager, "install", "chromium") == expected


@pytest.mark.unit
def test_playwright_command_handles_unparseable_and_empty_manager() -> None:
    # An unbalanced quote makes shlex raise; we fall back to the raw token (npx).
    """Verify playwright command handles unparseable and empty manager."""
    assert playwright_command('"', "test") == "npx playwright test"
    # An empty manager string yields no tokens and defaults to npx.
    assert playwright_command("", "test") == "npx playwright test"


@pytest.mark.unit
def test_browser_install_detects_node_manager_from_bare_install_token() -> None:
    # A bare manager token (no subcommand) still counts as an install signal.
    """Verify browser install detects node manager from bare install token."""
    profile = _profile({"runtime": {"browsers": ["chromium"]}, "phases": {"setup": ["yarn"]}})

    command = playwright_browser_install_command(profile)

    assert command is not None
    assert command.command == "yarn playwright install chromium"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("setup_command", "expected_command"),
    [
        ("/opt/pnpm/bin/pnpm install", "/opt/pnpm/bin/pnpm exec playwright install chromium"),
        (
            "/repo/.yarn/releases/yarn install",
            "/repo/.yarn/releases/yarn playwright install chromium",
        ),
    ],
)
def test_browser_install_preserves_path_qualified_node_manager(
    setup_command: str, expected_command: str
) -> None:
    """Verify browser install keeps path-qualified package managers from setup."""
    profile = _profile(
        {"runtime": {"browsers": ["chromium"]}, "phases": {"setup": [setup_command]}}
    )

    command = playwright_browser_install_command(profile)

    assert command is not None
    assert command.command == expected_command


@pytest.mark.unit
@pytest.mark.parametrize(
    ("setup_command", "expected_command"),
    [
        ("yarn --immutable", "yarn playwright install chromium"),
        ("yarn --immutable --immutable-cache", "yarn playwright install chromium"),
        (
            "yarn --cwd apps/console --immutable",
            "yarn --cwd apps/console playwright install chromium",
        ),
    ],
)
def test_browser_install_detects_yarn_option_only_install_commands(
    setup_command: str, expected_command: str
) -> None:
    """Verify browser install treats Yarn option-only CI installs as install signals."""
    profile = _profile(
        {"runtime": {"browsers": ["chromium"]}, "phases": {"setup": [setup_command]}}
    )

    command = playwright_browser_install_command(profile)

    assert command is not None
    assert command.command == expected_command


@pytest.mark.unit
def test_browser_install_ignores_yarn_option_only_help_queries() -> None:
    """Verify browser install ignores Yarn help/version queries without install subcommands."""
    profile = _profile(
        {"runtime": {"browsers": ["chromium"]}, "phases": {"setup": ["yarn --immutable --help"]}}
    )

    command = playwright_browser_install_command(profile)

    assert command is not None
    assert command.command == "npx playwright install chromium"


@pytest.mark.unit
def test_browser_install_ignores_node_manager_with_non_install_subcommand() -> None:
    # ``pnpm run build`` is not an install signal, so detection falls through to npx.
    """Verify browser install ignores node manager with non install subcommand."""
    profile = _profile(
        {"runtime": {"browsers": ["chromium"]}, "phases": {"setup": ["pnpm run build"]}}
    )

    command = playwright_browser_install_command(profile)

    assert command is not None
    assert command.command == "npx playwright install chromium"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("setup_command", "expected_install_command"),
    [
        (
            "npm run build && pnpm install",
            "pnpm exec playwright install chromium",
        ),
        (
            "npm run build&&pnpm install",
            "pnpm exec playwright install chromium",
        ),
        (
            "npm --prefix apps/console run build && pnpm install",
            "pnpm exec playwright install chromium",
        ),
        (
            "npm --prefix apps/console run build&&pnpm install",
            "pnpm exec playwright install chromium",
        ),
        (
            "yarn build; pnpm install",
            "pnpm exec playwright install chromium",
        ),
        (
            "yarn build;pnpm install",
            "pnpm exec playwright install chromium",
        ),
    ],
)
def test_browser_install_detects_install_only_in_current_shell_segment(
    setup_command: str, expected_install_command: str
) -> None:
    """Verify chained setup commands do not leak install subcommands across shell segments."""
    profile = _profile(
        {"runtime": {"browsers": ["chromium"]}, "phases": {"setup": [setup_command]}}
    )

    command = playwright_browser_install_command(profile)

    assert command is not None
    assert command.command == expected_install_command


@pytest.mark.unit
def test_browser_install_skips_unparseable_commands_and_still_finds_python() -> None:
    # The malformed (unbalanced-quote) command is skipped by both the node-manager
    # scan and the python-interpreter scan; detection continues to ``uv run``.
    """Verify browser install skips unparseable commands and still finds python."""
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
    """Verify browser install is none without declared browsers."""
    assert playwright_browser_install_command(_profile({"phases": {"setup": ["npm ci"]}})) is None


@pytest.mark.unit
def test_browser_install_does_not_infer_package_manager_from_pre_agent() -> None:
    # Browser install is appended to setup before pre_agent runs; only setup hooks
    # may influence package-manager detection.
    """Verify browser install does not infer package manager from pre agent."""
    profile = _profile(
        {"runtime": {"browsers": ["chromium"]}, "phases": {"pre_agent": ["pnpm install"]}}
    )

    command = playwright_browser_install_command(profile)

    assert command is not None
    assert command.command == "npx playwright install chromium"


@pytest.mark.unit
def test_setup_plan_runs_npx_browser_install_before_pre_agent_install() -> None:
    """Verify setup plan runs npx browser install before pre agent install."""
    profile = _profile(
        {"runtime": {"browsers": ["chromium"]}, "phases": {"pre_agent": ["pnpm install"]}}
    )

    plan = profile_phase_command_plan(profile, ["setup", "pre_agent"])
    commands = [step.command.command for step in plan]

    assert commands == ["npx playwright install chromium", "pnpm install"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("validate_command", "expected_command"),
    [
        ("pnpm install", "pnpm exec playwright install chromium"),
        ("npm ci", "npx playwright install chromium"),
        (
            "cd apps/console && pnpm install",
            "cd apps/console && pnpm exec playwright install chromium",
        ),
    ],
)
def test_browser_install_detects_node_manager_from_validate_only(
    validate_command: str, expected_command: str
) -> None:
    """Verify browser install infers Node package manager from validate when setup has none."""
    profile = _profile(
        {"runtime": {"browsers": ["chromium"]}, "phases": {"validate": [validate_command]}}
    )

    command = playwright_browser_install_command(profile)

    assert command is not None
    assert command.command == expected_command


@pytest.mark.unit
def test_browser_install_prefers_setup_node_manager_over_validate() -> None:
    """Verify setup install commands win when both setup and validate declare a manager."""
    profile = _profile(
        {
            "runtime": {"browsers": ["chromium"]},
            "phases": {"setup": ["pnpm install"], "validate": ["npm ci"]},
        }
    )

    command = playwright_browser_install_command(profile)

    assert command is not None
    assert command.command == "pnpm exec playwright install chromium"


@pytest.mark.unit
def test_browser_install_prefers_detected_node_package_manager() -> None:
    """Verify browser install prefers detected node package manager."""
    profile = _profile(
        {"runtime": {"browsers": ["chromium", "firefox"]}, "phases": {"setup": ["pnpm install"]}}
    )

    command = playwright_browser_install_command(profile)

    assert command is not None
    assert command.command == "pnpm exec playwright install chromium firefox"
    assert command.timeout_seconds == _BROWSER_INSTALL_TIMEOUT
    assert command.required is True


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
        (
            "cd apps/console && pnpm install",
            "cd apps/console && pnpm exec playwright install chromium",
        ),
        (
            "cd apps/console && CI=true pnpm install",
            "cd apps/console && pnpm exec playwright install chromium",
        ),
        (
            "cd apps/console; yarn install",
            "cd apps/console; yarn playwright install chromium",
        ),
        (
            "pnpm -C apps/console install",
            "pnpm -C apps/console exec playwright install chromium",
        ),
        (
            "pnpm -w install",
            "pnpm -w exec playwright install chromium",
        ),
        (
            "npm install -w apps/console",
            "npm -w apps/console exec playwright install chromium",
        ),
        (
            "npm -w apps/console ci",
            "npm -w apps/console exec playwright install chromium",
        ),
        (
            "npm --workspace apps/console ci",
            "npm --workspace apps/console exec playwright install chromium",
        ),
        (
            "cd apps && npm --prefix console ci",
            "cd apps && npm --prefix console exec playwright install chromium",
        ),
        (
            "cd apps; pnpm --dir console install",
            "cd apps; pnpm --dir console exec playwright install chromium",
        ),
    ],
)
def test_browser_install_preserves_package_manager_scope(
    setup_command: str, expected_install_command: str
) -> None:
    """Verify browser install preserves package manager scope."""
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
        (
            "uv --project apps/api run pytest",
            "uv --project apps/api run python",
        ),
        (
            "uv --directory apps/api run pytest",
            "uv --directory apps/api run python",
        ),
        (
            "uv --project=apps/api run pytest",
            "uv --project=apps/api run python",
        ),
        (
            "uv run --with pytest-playwright pytest",
            "uv run --with pytest-playwright python",
        ),
        (
            "uv run --with-requirements requirements-dev.txt pytest",
            "uv run --with-requirements requirements-dev.txt python",
        ),
        (
            "uv run -w pytest-playwright pytest",
            "uv run -w pytest-playwright python",
        ),
        (
            "uv run --default-index https://mirror/simple pytest",
            "uv run --default-index https://mirror/simple python",
        ),
        (
            "uv run --index-url https://mirror/simple pytest",
            "uv run --index-url https://mirror/simple python",
        ),
        (
            "uv --cache-dir .uv-cache run pytest",
            "uv --cache-dir .uv-cache run python",
        ),
        (
            "uv --color never run pytest",
            "uv --color never run python",
        ),
    ],
)
def test_browser_install_preserves_uv_run_project_scope(
    validate_command: str, expected_executable_prefix: str
) -> None:
    """Verify browser install preserves uv run project scope."""
    profile = _profile(
        {"runtime": {"browsers": ["chromium"]}, "phases": {"validate": [validate_command]}}
    )

    command = playwright_browser_install_command(profile)

    assert command is not None
    assert command.command == f"{expected_executable_prefix} -m playwright install chromium"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("setup_command", "expected_executable_prefix"),
    [
        ("uv sync --extra dev", "uv run --extra dev python"),
        (
            "uv sync --no-install-project --extra dev",
            "uv run --extra dev python",
        ),
        (
            "uv sync --no-install-package foo --extra dev",
            "uv run --extra dev python",
        ),
        (
            "uv sync --script foo.py --extra dev",
            "uv run --extra dev python",
        ),
        (
            "uv sync --config-setting foo=bar --extra dev",
            "uv run --config-setting foo=bar --extra dev python",
        ),
        ("uv sync --group dev", "uv run --group dev python"),
        ("uv sync --all-extras", "uv run --all-extras python"),
        ("uv sync --all-groups --no-dev", "uv run --all-groups --no-dev python"),
        (
            "uv sync --all-extras && uv run pytest",
            "uv run --all-extras python",
        ),
        ("uv --project apps/api sync", "uv --project apps/api run python"),
        (
            "uv --directory apps/api sync --extra dev",
            "uv --directory apps/api run --extra dev python",
        ),
        ('uv pip install -e ".[dev]"', "uv run python"),
        ("uv --directory apps/api pip install -e .", "uv --directory apps/api run python"),
        ("uv pip install --python 3.12 httpx", "uv run --python 3.12 python"),
        (
            "uv --cache-dir .uv-cache sync --extra dev",
            "uv --cache-dir .uv-cache run --extra dev python",
        ),
        (
            "uv --color never sync",
            "uv --color never run python",
        ),
    ],
)
def test_browser_install_preserves_uv_setup_project_scope(
    setup_command: str, expected_executable_prefix: str
) -> None:
    """Verify browser install preserves uv setup project scope."""
    profile = _profile(
        {
            "runtime": {"browsers": ["chromium"]},
            "phases": {"setup": [setup_command], "validate": ["pytest -q"]},
        }
    )

    command = playwright_browser_install_command(profile)

    assert command is not None
    assert command.command == f"{expected_executable_prefix} -m playwright install chromium"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("setup_command", "expected_executable"),
    [
        ("uv pip install --system playwright", "python"),
        ("uv pip install --system -e .", "python"),
        ("uv pip install --python 3.12 --system httpx", "python3.12"),
        ("uv pip install --system --python 3.12 httpx", "python3.12"),
        ("uv pip install --system --python python3.12 httpx", "python3.12"),
        (
            "uv pip install --system --python /usr/bin/python3.12 httpx",
            "/usr/bin/python3.12",
        ),
        (
            "uv pip install --system --python=/usr/bin/python3.12 httpx",
            "/usr/bin/python3.12",
        ),
        ("cd apps/api && uv pip install --system -e .", "python"),
    ],
)
def test_browser_install_uses_system_python_for_uv_pip_system(
    setup_command: str, expected_executable: str
) -> None:
    """Verify browser install uses system python for uv pip system."""
    profile = _profile(
        {
            "runtime": {"browsers": ["chromium"]},
            "phases": {"setup": [setup_command], "validate": ["pytest -q"]},
        }
    )

    command = playwright_browser_install_command(profile)

    assert command is not None
    if "cd " in setup_command:
        assert (
            command.command
            == f"cd apps/api && {expected_executable} -m playwright install chromium"
        )
    else:
        assert command.command == f"{expected_executable} -m playwright install chromium"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("profile_command", "phase", "expected_install_command"),
    [
        (
            "cd apps/api && uv run pytest",
            "validate",
            "cd apps/api && uv run python -m playwright install chromium",
        ),
        (
            "cd apps/api; uv sync",
            "setup",
            "cd apps/api; uv run python -m playwright install chromium",
        ),
        (
            "cd apps/api && python3 -m pytest",
            "validate",
            "cd apps/api && python3 -m playwright install chromium",
        ),
        (
            "cd apps/api && UV_PROJECT_ENVIRONMENT=.venv uv run pytest",
            "validate",
            "cd apps/api && uv run python -m playwright install chromium",
        ),
    ],
)
def test_browser_install_preserves_cd_scope_for_python(
    profile_command: str, phase: str, expected_install_command: str
) -> None:
    """Verify browser install preserves cd scope for python."""
    profile = _profile(
        {
            "runtime": {"browsers": ["chromium"]},
            "phases": {phase: [profile_command]},
        }
    )

    command = playwright_browser_install_command(profile)

    assert command is not None
    assert command.command == expected_install_command


@pytest.mark.unit
def test_browser_install_uses_python_interpreter_when_no_node_manager() -> None:
    """Verify browser install uses python interpreter when no node manager."""
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
@pytest.mark.parametrize(
    ("setup_command", "expected_executable"),
    [
        ("pip install -r requirements.txt", "python"),
        ("pip3 install playwright", "python3"),
        ("pip3.12 install -r requirements.txt", "python3.12"),
        (".venv/bin/pip install -r requirements.txt", ".venv/bin/python"),
        ("/opt/py/bin/pip3.12 install playwright", "/opt/py/bin/python3.12"),
        ("cd apps/api && pip install -e .", "python"),
        ("cd apps/api && .venv/bin/pip install -e .", ".venv/bin/python"),
    ],
)
def test_browser_install_recognizes_pip_setup(setup_command: str, expected_executable: str) -> None:
    """Verify browser install recognizes bare and path-qualified pip setup."""
    profile = _profile(
        {
            "runtime": {"browsers": ["chromium"]},
            "phases": {"setup": [setup_command], "validate": ["pytest -q"]},
        }
    )

    command = playwright_browser_install_command(profile)

    assert command is not None
    assert (
        command.command == f"{expected_executable} -m playwright install chromium"
        if "cd " not in setup_command
        else f"cd apps/api && {expected_executable} -m playwright install chromium"
    )


@pytest.mark.unit
def test_browser_install_prefers_uv_setup_over_pytest_playwright_validate_path() -> None:
    # A validate path containing "playwright" must not win over setup ``uv sync`` scope.
    """Verify browser install prefers uv setup over pytest playwright validate path."""
    profile = _profile(
        {
            "runtime": {"browsers": ["chromium"]},
            "phases": {
                "setup": ["uv sync --extra dev"],
                "validate": ["pytest tests/playwright"],
            },
        }
    )

    command = playwright_browser_install_command(profile)

    assert command is not None
    assert command.command == "uv run --extra dev python -m playwright install chromium"


@pytest.mark.unit
def test_browser_install_recognizes_bare_python_and_pytest_playwright() -> None:
    """Verify browser install recognizes bare python and pytest playwright."""
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
    """Verify browser install falls back to npx."""
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
    """Verify browser install does not treat node playwright cli as python."""
    profile = _profile(
        {"runtime": {"browsers": ["chromium"]}, "phases": {"validate": [validate_command]}}
    )

    command = playwright_browser_install_command(profile)

    assert command is not None
    assert command.command == "npx playwright install chromium"


@pytest.mark.unit
def test_setup_plan_appends_browser_install_after_setup_and_db_hooks() -> None:
    """Verify setup plan appends browser install after setup and db hooks."""
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
    """Verify setup plan omits browser install when no browsers declared."""
    profile = _profile({"phases": {"setup": ["npm ci"]}})

    plan = profile_phase_command_plan(profile, ["setup"])

    assert all("playwright install" not in step.command.command for step in plan)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("tokens", "expected"),
    [
        # A token after the PM ending with ``;`` contributes its non-empty prefix, then stops.
        (["yarn", "install;", "test"], ["install"]),
        # A bare ``;`` separator token just terminates scanning.
        (["yarn", ";", "install"], []),
    ],
)
def test_pm_invocation_tokens_handles_trailing_separator(
    tokens: list[str], expected: list[str]
) -> None:
    """Verify invocation token collection stops at embedded shell separators."""
    assert _pm_invocation_tokens(tokens, 0) == expected


@pytest.mark.unit
def test_collect_pm_scope_tokens_stops_at_shell_separator_token() -> None:
    """Verify scope collection stops when a bare ``&&`` separator token appears."""
    # ``-w`` is a pnpm boolean scope flag; ``&&`` terminates the PM invocation.
    assert _collect_pm_scope_tokens(["pnpm", "-w", "&&", "install"], 0, pm_base="pnpm") == ["-w"]


@pytest.mark.unit
def test_collect_pm_scope_tokens_value_flag_followed_by_dash_keeps_only_flag() -> None:
    """Verify a value flag whose next token is another option is kept alone (no value consumed)."""
    # ``npm -w`` selects a workspace; the following ``-g`` is another option, so
    # only ``-w`` is collected as scope and scanning continues.
    assert _collect_pm_scope_tokens(["npm", "-w", "-g", "install"], 0, pm_base="npm") == ["-w"]


@pytest.mark.unit
def test_collect_pm_scope_tokens_value_flag_at_end_keeps_only_flag() -> None:
    """Verify a trailing value flag with no following token is collected alone."""
    assert _collect_pm_scope_tokens(["npm", "-w"], 0, pm_base="npm") == ["-w"]


@pytest.mark.unit
def test_collect_pm_scope_tokens_stops_at_double_dash() -> None:
    """Verify ``--`` terminates scope collection without being collected."""
    assert _collect_pm_scope_tokens(["npm", "--", "install"], 0, pm_base="npm") == []


@pytest.mark.unit
def test_collect_pm_scope_tokens_boolean_flag_with_trailing_semicolon_terminates() -> None:
    """Verify a boolean scope flag carrying a trailing ``;`` is collected then scanning ends."""
    # ``pnpm -w;`` — ``-w`` is a pnpm boolean scope flag; the ``;`` ends the chain.
    assert _collect_pm_scope_tokens(["pnpm", "-w;", "install"], 0, pm_base="pnpm") == ["-w"]


@pytest.mark.unit
def test_collect_pm_scope_tokens_value_flag_consumes_trailing_semicolon_value() -> None:
    """Verify a value flag consumes a following ``value;`` token (semicolon ends scanning)."""
    # ``npm -w`` is a value flag; ``apps;`` is its value and the trailing ``;`` ends scanning.
    assert _collect_pm_scope_tokens(["npm", "-w", "apps;", "install"], 0, pm_base="npm") == [
        "-w",
        "apps;",
    ]


@pytest.mark.unit
def test_collect_pm_scope_tokens_empty_trailing_semicolon_breaks() -> None:
    """Verify a bare ``;`` separator token ends scope collection."""
    # ``pnpm -w`` is boolean scope; a bare ``;`` separator stops scanning.
    assert _collect_pm_scope_tokens(["pnpm", "-w", ";", "install"], 0, pm_base="pnpm") == ["-w"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("terminator", "expected"),
    [
        (";", "cd apps; "),
        ("&&", "cd apps && "),
        ("||", "cd apps || "),
    ],
)
def test_cd_prefix_from_cd_only_segment_builds_scoped_prefix(
    terminator: str, expected: str
) -> None:
    """Verify a ``cd <dir>``-only segment yields a scoped cd prefix with its terminator."""
    assert _cd_prefix_from_cd_only_segment("cd apps", terminator) == expected


@pytest.mark.unit
def test_cd_prefix_from_cd_only_segment_rejects_non_cd_and_empty() -> None:
    """Verify non-cd segments, empty segments, and empty terminators are not cd-only."""
    assert _cd_prefix_from_cd_only_segment("echo hi", ";") is None
    assert _cd_prefix_from_cd_only_segment("cd apps", "") is None
    assert _cd_prefix_from_cd_only_segment("", ";") is None
    assert _cd_prefix_from_cd_only_segment("cd apps extra", ";") is None


@pytest.mark.unit
def test_cd_prefix_from_cd_only_segment_rejects_unparseable() -> None:
    """Verify an unparseable cd segment (unbalanced quote) yields no prefix."""
    assert _cd_prefix_from_cd_only_segment("cd 'unterminated", ";") is None


@pytest.mark.unit
def test_extract_cd_scope_prefix_skips_env_assignments_before_pm() -> None:
    """Verify env assignments (FOO=bar) between ``cd`` and the PM are skipped."""
    # Tokens: cd apps && FOO=bar pnpm install — pm_index points at pnpm.
    assert _extract_cd_scope_prefix(["cd", "apps", "&&", "FOO=bar", "pnpm"], 4) == "cd apps && "


@pytest.mark.unit
def test_extract_cd_scope_prefix_returns_none_without_cd() -> None:
    """Verify no ``cd`` preceding the PM yields no prefix."""
    assert _extract_cd_scope_prefix(["npm", "install"], 1) is None


@pytest.mark.unit
def test_has_pip_install_subcommand_stops_at_double_dash() -> None:
    """Verify ``--`` before ``install`` means no install subcommand is present."""
    assert _has_pip_install_subcommand(["pip", "--", "install"], 0) is False
    assert _has_pip_install_subcommand(["pip", "install", "--", "x"], 0) is True
    assert _has_pip_install_subcommand(["pip", "run"], 0) is False


@pytest.mark.unit
@pytest.mark.parametrize(
    ("tokens", "expected"),
    [
        # Trailing ``--immutable;`` (semicolon-terminated) still counts as install signal.
        (["yarn", "--immutable;"], True),
        # A ``--help;`` query after the PM is not an install signal.
        (["yarn", "--help;"], False),
        # A bare chain separator terminates scanning (install flag seen before it).
        (["yarn", "--immutable", "&&", "build"], True),
    ],
)
def test_is_node_option_only_install_command_handles_semicolons_and_separators(
    tokens: list[str], expected: bool
) -> None:
    """Verify option-only install detection respects trailing separators and query flags."""
    assert _is_node_option_only_install_command(tokens, 0, "yarn") is expected


@pytest.mark.unit
def test_is_node_option_only_install_command_returns_false_for_trailing_scope_value_flag() -> None:
    """Verify a trailing scope value flag with no argument is not an install signal."""
    # ``yarn --cwd`` (value flag, no following token) → no install flag seen → False.
    assert _is_node_option_only_install_command(["yarn", "--cwd"], 0, "yarn") is False


@pytest.mark.unit
def test_is_node_option_only_install_command_returns_false_for_non_yarn_base() -> None:
    """Verify only Yarn has option-only install flags; other PMs return False immediately."""
    assert _is_node_option_only_install_command(["pnpm", "--immutable"], 0, "pnpm") is False


@pytest.mark.unit
@pytest.mark.parametrize(
    ("tokens", "expected"),
    [
        # Trailing value flag with no argument.
        (["sync", "--python"], ["--python"]),
        # Value flag followed by another option (kept alone).
        (["sync", "--python", "--x"], ["--python"]),
        # Unknown option consumes its value, then real scope flags follow.
        (["sync", "--foo", "bar", "--extra", "dev"], ["--extra", "dev"]),
    ],
)
def test_collect_uv_sync_scope_tokens_handles_value_and_unknown_options(
    tokens: list[str], expected: list[str]
) -> None:
    """Verify ``uv sync`` scope collection handles trailing values and unknown options."""
    assert _collect_uv_sync_scope_tokens(tokens, 0) == expected


@pytest.mark.unit
def test_collect_uv_pip_scope_tokens_stops_at_double_dash() -> None:
    """Verify ``--`` terminates ``uv pip`` scope collection."""
    assert _collect_uv_pip_scope_tokens(["install", "--", "--python"], 0) == []


@pytest.mark.unit
@pytest.mark.parametrize(
    ("tokens", "expected"),
    [
        # Unknown option with ``=`` value, then ``--system`` → system python.
        (["install", "--foo=bar", "--system"], "python"),
        # Trailing unknown option with no value and no ``--system`` → None.
        (["install", "--foo"], None),
        # ``--system`` then a trailing ``--python`` value flag with no arg → bare python.
        (["install", "--system", "--python"], "python"),
    ],
)
def test_uv_pip_system_python_executable_edge_cases(
    tokens: list[str], expected: str | None
) -> None:
    """Verify ``uv pip install --system`` resolves the python executable across option shapes."""
    assert _uv_pip_system_python_executable(tokens, 0) == expected


@pytest.mark.unit
def test_uv_pip_system_python_executable_returns_path_for_python_equals_path() -> None:
    """Verify ``--python=/usr/bin/python3.12 --system`` returns that absolute interpreter."""
    assert (
        _uv_pip_system_python_executable(["install", "--system", "--python=/usr/bin/python3.12"], 0)
        == "/usr/bin/python3.12"
    )


@pytest.mark.unit
def test_uv_setup_python_prefix_returns_none_for_unknown_pip_subcommand() -> None:
    """Verify ``uv pip freeze`` (not install|sync) yields no python prefix."""
    assert _uv_setup_python_prefix(["uv", "pip", "freeze"], 0) is None


@pytest.mark.unit
def test_python_executable_from_commands_skips_unparseable_command() -> None:
    """Verify an unparseable command (unbalanced quote) is skipped without matching."""
    # The unterminated quote fails shlex.split; the runnable ``uv run`` command wins.
    result = _python_executable_from_commands(
        ['echo "unterminated', "uv run pytest"], allow_pytest_playwright_shortcut=False
    )
    assert result is not None
    assert result[0] == "uv run python"


@pytest.mark.unit
def test_uv_run_python_prefix_returns_none_when_run_missing_after_global_scope() -> None:
    """Verify a uv invocation with global scope but no ``run`` subcommand yields no prefix."""
    assert _uv_run_python_prefix(["uv", "--project", "apps"], 0) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    ("tokens", "expected"),
    [
        # ``--`` ends global options and invalidates the run prefix.
        (["uv", "--", "run", "pytest"], None),
        # A trailing global value flag with no argument invalidates the prefix.
        (["uv", "--project"], None),
    ],
)
def test_collect_uv_global_scope_tokens_invalidates_on_dash_and_trailing_value(
    tokens: list[str], expected: tuple[list[str], int] | None
) -> None:
    """Verify ``--`` and a trailing value flag invalidate the uv global scope."""
    assert _collect_uv_global_scope_tokens(tokens, 0) == expected


@pytest.mark.unit
def test_collect_uv_global_scope_tokens_keeps_value_flag_followed_by_option() -> None:
    """Verify a global value flag whose next token is an option is kept without consuming it."""
    # ``--project`` is followed by ``--x`` (another option): kept alone, scanning continues to ``run``.
    assert _collect_uv_global_scope_tokens(["uv", "--project", "--x", "run", "pytest"], 0) == (
        ["--project", "--x"],
        3,
    )


@pytest.mark.unit
def test_collect_uv_sync_scope_tokens_value_flag_followed_by_dash_keeps_only_flag() -> None:
    """Verify a ``uv sync`` value flag followed by another option keeps only the flag."""
    assert _collect_uv_sync_scope_tokens(["sync", "--python", "--x"], 0) == ["--python"]


@pytest.mark.unit
def test_collect_uv_sync_scope_tokens_stops_at_double_dash() -> None:
    """Verify ``--`` terminates ``uv sync`` scope collection."""
    assert _collect_uv_sync_scope_tokens(["sync", "--", "--extra"], 0) == []


@pytest.mark.unit
def test_collect_uv_pip_scope_tokens_breaks_on_non_scope_option() -> None:
    """Verify an option not in the ``uv pip`` scope set terminates scope collection."""
    assert _collect_uv_pip_scope_tokens(["install", "--unknown"], 0) == []


@pytest.mark.unit
def test_uv_pip_system_python_executable_returns_none_without_system_flag() -> None:
    """Verify ``uv pip install`` without ``--system`` yields no system python."""
    assert _uv_pip_system_python_executable(["install", "--python", "3.12"], 0) is None


@pytest.mark.unit
def test_uv_pip_system_python_executable_handles_unknown_equals_value_with_system() -> None:
    """Verify an unknown ``--foo=bar`` option with ``--system`` still returns system python."""
    assert _uv_pip_system_python_executable(["install", "--system", "--foo=bar"], 0) == "python"


@pytest.mark.unit
def test_uv_pip_system_python_executable_handles_trailing_unknown_option_with_system() -> None:
    """Verify a trailing unknown option after ``--system`` still returns system python."""
    assert _uv_pip_system_python_executable(["install", "--system", "--foo"], 0) == "python"


@pytest.mark.unit
def test_is_node_option_only_install_command_returns_false_for_trailing_double_dash_semicolon() -> (
    None
):
    """Verify a ``--;`` token (strips to ``--``) is not an install signal."""
    assert _is_node_option_only_install_command(["yarn", "--;"], 0, "yarn") is False


@pytest.mark.unit
def test_detected_node_package_manager_skips_empty_statements() -> None:
    """Verify empty shell statements (e.g. trailing ``;;``) do not crash node detection."""
    profile = _profile(
        {"runtime": {"browsers": ["chromium"]}, "phases": {"setup": ["pnpm install;;"]}}
    )
    package_manager, cd_prefix = _detected_node_package_manager(profile)
    assert package_manager == "pnpm"
    assert cd_prefix is None


@pytest.mark.unit
def test_uv_run_python_prefix_returns_none_when_global_scope_is_invalid() -> None:
    """Verify a uv invocation whose global scope is invalid (``--``) yields no run prefix."""
    assert _uv_run_python_prefix(["uv", "--", "run", "pytest"], 0) is None


@pytest.mark.unit
def test_uv_setup_python_prefix_returns_none_when_global_scope_exhausts_tokens() -> None:
    """Verify a uv invocation whose global scope consumes all tokens yields no python prefix."""
    # ``uv --project x`` — global scope consumes both flags; no subcommand remains.
    assert _uv_setup_python_prefix(["uv", "--project", "x"], 0) is None


@pytest.mark.unit
def test_uv_setup_python_prefix_returns_none_without_subcommand() -> None:
    """Verify a uv invocation with global scope but no subcommand yields no python prefix."""
    assert _uv_setup_python_prefix(["uv", "--project", "x"], 0) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    ("tokens", "expected"),
    [
        (["install", "--system", "--python"], "python"),
        (["install", "-p", "3.12", "--system"], "python3.12"),
    ],
)
def test_uv_pip_system_python_executable_handles_python_value_flags(
    tokens: list[str], expected: str
) -> None:
    """Verify ``uv pip install --system`` resolves ``--python``/``-p`` value flags."""
    assert _uv_pip_system_python_executable(tokens, 0) == expected


@pytest.mark.unit
def test_detected_node_package_manager_skips_unparseable_cd_segment() -> None:
    """Verify an unparseable cd-scoped command does not crash node-manager detection."""
    # The unterminated quote makes the whole statement one segment; shlex fails so
    # the node scan skips it and detection falls through (no node PM found).
    profile = _profile(
        {
            "runtime": {"browsers": ["chromium"]},
            "phases": {"setup": ["cd 'unterminated && pnpm install"]},
        }
    )
    assert _detected_node_package_manager(profile) == (None, None)


@pytest.mark.unit
def test_browser_install_detects_node_manager_from_database_generated_setup() -> None:
    """Verify database-generated setup commands are scanned for the node package manager."""
    profile = _profile(
        {
            "runtime": {"browsers": ["chromium"]},
            "phases": {"setup": ["echo ready"]},
            "database": {"generated_setup": ["pnpm install"]},
        }
    )

    command = playwright_browser_install_command(profile)

    assert command is not None
    assert command.command == "pnpm exec playwright install chromium"


@pytest.mark.unit
def test_pm_invocation_tokens_stops_at_shell_chain_separators() -> None:
    """PM invocation parsing must not bleed into chained shell segments."""
    tokens = ["pnpm", "install", "&&", "npm", "test"]
    assert _pm_invocation_tokens(tokens, 0) == ["install"]
    assert _pm_invocation_tokens(["yarn", "add", "pkg;"], 0) == ["add", "pkg"]
    assert _pm_invocation_tokens(["npm", "ci", ";"], 0) == ["ci"]


@pytest.mark.unit
def test_collect_pm_scope_tokens_handles_non_consecutive_and_terminators() -> None:
    """Scope extraction must preserve cwd flags and stop at shell terminators."""
    tokens = shlex.split("pnpm -C apps install --filter pkg && npm test")
    pnpm_index = tokens.index("pnpm")
    assert _collect_pm_scope_tokens(tokens, pnpm_index) == ["-C", "apps"]

    yarn_tokens = shlex.split("yarn --cwd apps --immutable; echo done")
    yarn_index = yarn_tokens.index("yarn")
    assert _collect_pm_scope_tokens(yarn_tokens, yarn_index) == ["--cwd", "apps"]

    npm_tokens = ["npm", "-w", "pkg", "install", "leftover"]
    assert _collect_pm_scope_tokens(npm_tokens, 0, require_consecutive=False) == ["-w", "pkg"]

    pnpm_boolean = shlex.split("pnpm -w install;")
    assert _collect_pm_scope_tokens(pnpm_boolean, 0) == ["-w"]

    npm_missing_value = ["npm", "-w"]
    assert _collect_pm_scope_tokens(npm_missing_value, 0) == ["-w"]


@pytest.mark.unit
def test_collect_uv_helper_scope_parsers_cover_global_run_and_pip_paths() -> None:
    """UV scope helpers must preserve project/python selectors for browser install."""
    global_tokens = shlex.split("uv --project apps run --python 3.12 python -m playwright install")
    uv_index = global_tokens.index("uv")
    collected = _collect_uv_global_scope_tokens(global_tokens, uv_index)
    assert collected == (["--project", "apps"], global_tokens.index("run"))

    sync_tokens = shlex.split("uv sync --frozen --extra docs")
    sync_index = sync_tokens.index("sync")
    assert _collect_uv_sync_scope_tokens(sync_tokens, sync_index) == ["--frozen", "--extra", "docs"]

    pip_tokens = shlex.split("uv pip install --python 3.12 --system requests")
    pip_index = pip_tokens.index("pip") + 1
    assert _collect_uv_pip_scope_tokens(pip_tokens, pip_index) == ["--python", "3.12"]
    assert _uv_pip_system_python_executable(pip_tokens, pip_index) == "python3.12"

    assert _uv_run_python_prefix(global_tokens, uv_index) == (
        "uv --project apps run --python 3.12 python"
    )
    setup_tokens = shlex.split("uv --directory apps sync --frozen")
    assert _uv_setup_python_prefix(setup_tokens, setup_tokens.index("uv")) == (
        "uv --directory apps run --frozen python"
    )


@pytest.mark.unit
def test_uv_helper_scope_parsers_fail_closed_on_invalid_tokens() -> None:
    """Invalid UV token shapes must not invent browser-install prefixes."""
    assert _collect_uv_global_scope_tokens(["node", "run", "app"], 0) is None
    assert _collect_uv_global_scope_tokens(shlex.split("uv --"), 0) is None
    assert _uv_run_python_prefix(shlex.split("uv pip install pkg"), 0) is None
    assert _uv_setup_python_prefix(shlex.split("uv cache dir"), 0) is None
    assert _uv_pip_system_python_executable(shlex.split("uv pip install pkg"), 1) is None


@pytest.mark.unit
def test_cd_prefix_helpers_extract_shell_scoped_install_commands() -> None:
    """cd-only shell segments must preserve directory scope for browser install."""
    assert _cd_prefix_from_cd_only_segment("cd apps/web", "&&") == "cd apps/web && "
    assert _cd_prefix_from_cd_only_segment("cd apps/web", ";") == "cd apps/web; "
    assert _cd_prefix_from_cd_only_segment("cd apps", "|") is None

    tokens = shlex.split("cd apps && pnpm -C nested install")
    pm_index = tokens.index("pnpm")
    assert _extract_cd_scope_prefix(tokens, pm_index) == "cd apps && "


@pytest.mark.unit
def test_node_option_only_install_and_pip_helpers_cover_edge_paths() -> None:
    """Yarn immutable installs and pip executables must be recognized narrowly."""
    yarn_tokens = shlex.split("yarn --cwd apps --immutable")
    assert (
        _is_node_option_only_install_command(yarn_tokens, yarn_tokens.index("yarn"), "yarn") is True
    )
    assert _is_node_option_only_install_command(shlex.split("yarn --help"), 0, "yarn") is False
    assert _is_node_option_only_install_command(shlex.split("yarn --immutable;"), 0, "yarn") is True

    assert _pip_to_python_executable("pip3.12") == "python3.12"
    assert _pip_to_python_executable("node") is None

    pip_tokens = shlex.split("python -m pip install -r requirements.txt")
    pip_index = pip_tokens.index("pip")
    assert _has_pip_install_subcommand(pip_tokens, pip_index) is True
    assert _has_pip_install_subcommand(shlex.split("python -m pip --version"), 2) is False


@pytest.mark.unit
def test_playwright_command_collects_scope_from_tokenized_package_manager() -> None:
    """Path-qualified package managers must preserve scope flags in the emitted command."""
    assert (
        playwright_command("/opt/pnpm/bin/pnpm -C apps", "install", "chromium")
        == "/opt/pnpm/bin/pnpm -C apps exec playwright install chromium"
    )
    assert playwright_command("'unclosed", "install", "chromium") == (
        "npx playwright install chromium"
    )


@pytest.mark.unit
def test_detected_node_package_manager_handles_cd_only_and_validate_fallback() -> None:
    """cd-scoped installs and validate-only Node commands must be detected."""
    profile = _profile(
        {
            "phases": {
                "setup": ["cd apps/web;"],
                "validate_commands": ["cd apps/web && pnpm install"],
            }
        }
    )
    package_manager, cd_prefix = _detected_node_package_manager(profile)
    assert package_manager == "pnpm"
    assert cd_prefix == "cd apps/web && "

    pipe_scoped = _profile({"phases": {"setup": ["cd apps/web | pnpm install"]}})
    assert _detected_node_package_manager(pipe_scoped) == ("pnpm", None)


@pytest.mark.unit
def test_python_executable_from_commands_handles_uv_setup_and_bad_commands() -> None:
    """Python inference must skip unparseable commands and honor uv setup prefixes."""
    commands = [
        "notquoted",
        "uv --project apps sync --frozen",
        "cd svc && uv run --python 3.12 python -m playwright install",
    ]
    assert _python_executable_from_commands(commands, allow_pytest_playwright_shortcut=False) == (
        "uv --project apps run --frozen python",
        None,
    )

    pytest_shortcut = _python_executable_from_commands(
        ["pytest -q tests/playwright"],
        allow_pytest_playwright_shortcut=True,
    )
    assert pytest_shortcut == ("python", None)


@pytest.mark.unit
def test_extract_cd_scope_prefix_handles_inline_cd_and_chain_separators() -> None:
    """Inline ``cd <dir> && <pm>`` segments must preserve directory scope."""
    tokens = shlex.split("cd apps && pnpm install")
    assert _extract_cd_scope_prefix(tokens, tokens.index("pnpm")) == "cd apps && "

    semicolon_tokens = ["cd", "apps;", "pnpm", "install"]
    assert _extract_cd_scope_prefix(semicolon_tokens, semicolon_tokens.index("pnpm")) == (
        "cd apps; "
    )


@pytest.mark.unit
def test_playwright_browser_install_command_applies_cd_prefix_for_node_manager() -> None:
    """Generated browser install must keep cd scope from the detected install command."""
    profile = _profile(
        {
            "runtime": {"browsers": ["chromium"]},
            "phases": {"setup": ["cd apps/web && pnpm install"]},
        }
    )

    command = playwright_browser_install_command(profile)

    assert command is not None
    assert command.command == "cd apps/web && pnpm exec playwright install chromium"


@pytest.mark.unit
def test_collect_pm_scope_tokens_skips_value_flag_without_following_token() -> None:
    """Value flags without a trailing token must not consume unrelated argv."""
    npm_tokens = ["npm", "--cwd"]
    assert _collect_pm_scope_tokens(npm_tokens, 0) == ["--cwd"]

    chained_scope = shlex.split("pnpm --filter pkg;")
    assert _collect_pm_scope_tokens(chained_scope, 0) == ["--filter", "pkg;"]

    skipped_value = ["npm", "-w", "-C", "apps", "install"]
    assert _collect_pm_scope_tokens(skipped_value, 0, require_consecutive=False) == [
        "-w",
        "-C",
        "apps",
    ]


@pytest.mark.unit
def test_collect_uv_global_scope_tokens_requires_value_arguments() -> None:
    """UV global flags with missing values must fail closed."""
    tokens = shlex.split("uv --cache-dir")
    assert _collect_uv_global_scope_tokens(tokens, 0) is None

    hyphen_value = shlex.split("uv --cache-dir --project apps run python")
    collected = _collect_uv_global_scope_tokens(hyphen_value, 0)
    assert collected == (["--cache-dir", "--project", "apps"], hyphen_value.index("run"))


@pytest.mark.unit
def test_uv_setup_python_prefix_rejects_unknown_subcommands() -> None:
    """Only ``uv sync`` and ``uv pip install|sync`` may seed browser-install prefixes."""
    assert _uv_setup_python_prefix(shlex.split("uv pip wheel pkg"), 0) is None
    assert _uv_setup_python_prefix(shlex.split("uv cache prune"), 0) is None


@pytest.mark.unit
def test_playwright_scope_helpers_cover_remaining_branch_edges() -> None:
    """Cover fail-closed parser branches that browser-install planning relies on."""
    assert _pm_invocation_tokens(["npm", ";"], 0) == []
    assert _pm_invocation_tokens(["npm", "run;"], 0) == ["run"]

    assert _collect_pm_scope_tokens(["npm", ";"], 0) == []
    assert _collect_pm_scope_tokens(["npm", "--", "install"], 0) == []
    assert _collect_pm_scope_tokens(["npm", "leftover;", "-w"], 0, require_consecutive=False) == []
    assert _collect_pm_scope_tokens(shlex.split("pnpm -w;"), 0) == ["-w"]
    assert _collect_pm_scope_tokens(["npm", "-C;", "-w"], 0) == ["-C"]
    assert _collect_pm_scope_tokens(["npm", "-C", "-w"], 0) == ["-C", "-w"]
    assert _collect_pm_scope_tokens(shlex.split("pnpm -C apps;"), 0) == ["-C", "apps;"]
    assert _collect_pm_scope_tokens(["npm", "-C;", "apps"], 0) == ["-C", "apps"]
    assert _collect_pm_scope_tokens(["npm", "-w", "-"], 0) == ["-w"]
    assert _collect_pm_scope_tokens(shlex.split("pnpm --filter pkg"), 0) == ["--filter", "pkg"]
    assert _pm_invocation_tokens(["npm", "run;", "build"], 0) == ["run"]

    assert _collect_uv_global_scope_tokens(shlex.split("uv --"), 0) is None
    assert _uv_run_python_prefix(shlex.split("uv --project apps"), 0) is None
    assert _uv_run_python_prefix(shlex.split("uv --"), 0) is None

    sync_unknown = shlex.split("uv sync --unknown-flag value --frozen")
    sync_index = sync_unknown.index("sync")
    assert _collect_uv_sync_scope_tokens(sync_unknown, sync_index) == ["--frozen"]
    assert _collect_uv_sync_scope_tokens(["uv", "sync", "--python"], 1) == ["--python"]
    assert _collect_uv_sync_scope_tokens(["uv", "sync", "--python", "-"], 1) == ["--python"]
    assert _collect_uv_sync_scope_tokens(["uv", "sync", "--"], 1) == []

    pip_tokens = ["uv", "pip", "install", "--"]
    assert _collect_uv_pip_scope_tokens(pip_tokens, 2) == []
    assert _collect_uv_pip_scope_tokens(["uv", "pip", "install", "--python"], 2) == ["--python"]
    assert _collect_uv_pip_scope_tokens(["uv", "pip", "install", "--python", "3.12"], 2) == [
        "--python",
        "3.12",
    ]
    assert _collect_uv_pip_scope_tokens(["uv", "pip", "install", "--python", "-"], 2) == [
        "--python"
    ]
    assert _collect_uv_pip_scope_tokens(shlex.split("uv pip install --python=3.12 pkg"), 2) == [
        "--python=3.12"
    ]

    pip_system = shlex.split("uv pip install --system --python=3.12 pkg")
    assert _uv_pip_system_python_executable(pip_system, 2) == "python3.12"
    pip_system_spaced = shlex.split("uv pip install --system --python 3.12 pkg")
    assert _uv_pip_system_python_executable(pip_system_spaced, 2) == "python3.12"
    pip_system_eq = shlex.split("uv pip install --system --python=3.12 --reinstall pkg")
    assert _uv_pip_system_python_executable(pip_system_eq, 2) == "python3.12"
    pip_system_unknown = shlex.split("uv pip install --system --reinstall pkg")
    assert _uv_pip_system_python_executable(pip_system_unknown, 2) == "python"
    pip_system_eq_flag = shlex.split("uv pip install --system --config-setting=a=b pkg")
    assert _uv_pip_system_python_executable(pip_system_eq_flag, 2) == "python"
    pip_system_no_value = shlex.split("uv pip install --system --reinstall")
    assert _uv_pip_system_python_executable(pip_system_no_value, 2) == "python"
    assert _uv_pip_system_python_executable(["uv", "pip", "install", "--"], 2) is None
    pip_system_hyphen_python = shlex.split("uv pip install --system --python - pkg")
    assert _uv_pip_system_python_executable(pip_system_hyphen_python, 2) == "python"

    assert _cd_prefix_from_cd_only_segment("   ", "&&") is None

    assert _uv_setup_python_prefix(shlex.split("uv --project apps"), 0) is None
    assert _uv_setup_python_prefix(shlex.split("uv pip"), 0) is None
    assert _uv_setup_python_prefix(shlex.split("uv --"), 0) is None

    run_tokens = shlex.split("uv run --python 3.12 -- python -m playwright install")
    run_index = run_tokens.index("run")
    assert _collect_uv_run_scope_tokens(run_tokens, run_index) == ["--python", "3.12"]
    assert _collect_uv_run_scope_tokens(["uv", "run", "--python"], 1) == ["--python"]
    assert _collect_uv_run_scope_tokens(["uv", "run", "--python", "-"], 1) == ["--python", "-"]

    assert _cd_prefix_from_cd_only_segment("cd 'unclosed", "&&") is None
    assert _cd_prefix_from_cd_only_segment("cd apps extra", "&&") is None
    assert _cd_prefix_from_cd_only_segment("cd apps;", ";") == "cd apps; "

    env_scoped = shlex.split("FOO=bar cd apps && pnpm install")
    assert _extract_cd_scope_prefix(env_scoped, env_scoped.index("pnpm")) == "cd apps && "

    yarn_chain = shlex.split("yarn --immutable && npm test")
    assert (
        _is_node_option_only_install_command(yarn_chain, yarn_chain.index("yarn"), "yarn") is True
    )
    assert _is_node_option_only_install_command(["yarn", " ;"], 0, "yarn") is False
    assert _is_node_option_only_install_command(["yarn", "install"], 0, "yarn") is False
    assert _is_node_option_only_install_command(["yarn", "--help"], 0, "yarn") is False
    assert _is_node_option_only_install_command(["yarn", "--help;"], 0, "yarn") is False
    assert _is_node_option_only_install_command(["yarn", "--frozen;"], 0, "yarn") is False
    assert (
        _is_node_option_only_install_command(shlex.split("yarn --immutable --cwd"), 0, "yarn")
        is False
    )
    assert _is_node_option_only_install_command(shlex.split("yarn --immutable;"), 0, "yarn") is True

    cd_dir_named_pm = _profile({"phases": {"validate_commands": ["cd pnpm; pnpm install"]}})
    assert _detected_node_package_manager(cd_dir_named_pm) == ("pnpm", None)

    bad_cd_profile = _profile(
        {"phases": {"setup": ["cd 'unclosed;"], "validate_commands": ["pnpm install"]}}
    )
    assert _detected_node_package_manager(bad_cd_profile) == ("pnpm", None)
    cd_only_bad = _profile({"phases": {"validate_commands": ["cd apps/web; pnpm install"]}})
    assert _detected_node_package_manager(cd_only_bad) == ("pnpm", "cd apps/web; ")
    cd_names_pm_profile = _profile({"phases": {"validate_commands": ["cd pnpm"]}})
    assert _detected_node_package_manager(cd_names_pm_profile) == ("pnpm", None)

    assert _pip_to_python_executable("/opt/venv/bin/pip3.12") == "/opt/venv/bin/python3.12"
    pip_with_separator = shlex.split("python -m pip -- install pkg")
    assert _has_pip_install_subcommand(shlex.split("pip wheel -- install"), 0) is False
    assert _has_pip_install_subcommand(pip_with_separator, pip_with_separator.index("pip")) is False

    assert (
        _python_executable_from_commands(["notquoted"], allow_pytest_playwright_shortcut=False)
        is None
    )
    assert (
        _python_executable_from_commands(
            ["npm install 'unclosed"],
            allow_pytest_playwright_shortcut=False,
        )
        is None
    )
    assert _python_executable_from_commands(
        ["uv sync --frozen"],
        allow_pytest_playwright_shortcut=False,
    ) == ("uv run --frozen python", None)
    assert _python_executable_from_commands(
        ["uv pip install --system --python 3.12 pkg"],
        allow_pytest_playwright_shortcut=False,
    ) == ("python3.12", None)
    assert _python_executable_from_commands(
        ["uv cache dir", "uv sync --frozen"],
        allow_pytest_playwright_shortcut=False,
    ) == ("uv run --frozen python", None)
    uv_sync_profile = _profile({"phases": {"setup": ["uv sync --frozen"]}})
    assert _python_playwright_executable(uv_sync_profile) == ("uv run --frozen python", None)


@pytest.mark.unit
def test_node_option_only_install_rejects_bare_semicolon_terminator() -> None:
    """A bare ``;`` after the PM token must not count as an install-only flag."""
    assert _is_node_option_only_install_command(["yarn", ";"], 0, "yarn") is False


@pytest.mark.unit
def test_detected_node_package_manager_ignores_unparseable_cd_only_segment() -> None:
    """Unparseable ``cd`` segments must not become a pending directory scope prefix."""
    profile = _profile(
        {"phases": {"setup": ["cd 'unclosed"], "validate_commands": ["pnpm install"]}}
    )
    assert _detected_node_package_manager(profile) == ("pnpm", None)
