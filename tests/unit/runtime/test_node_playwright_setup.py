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
def test_setup_plan_omits_browser_install_when_already_in_setup() -> None:
    """Explicit setup browser-install is not duplicated in the command plan."""
    profile = _profile(
        {
            "runtime": {"browsers": ["chromium"]},
            "phases": {
                "setup": ["npm ci", "npx playwright install chromium"],
            },
        }
    )

    plan = profile_phase_command_plan(profile, ["setup"])
    commands = [step.command.command for step in plan]

    assert commands == ["npm ci", "npx playwright install chromium"]


@pytest.mark.unit
def test_setup_plan_preserves_explicit_browser_install_order_before_dependent_setup() -> None:
    """Required explicit browser-install must stay before later setup hooks that depend on it."""
    profile = _profile(
        {
            "runtime": {"browsers": ["chromium"]},
            "phases": {
                "setup": [
                    "npx playwright install chromium",
                    "node scripts/verify-chromium.js",
                ],
            },
            "database": {"generated_setup": ["psql -c 'select 1'"]},
        }
    )

    plan = profile_phase_command_plan(profile, ["setup"])
    commands = [step.command.command for step in plan]

    assert commands == [
        "npx playwright install chromium",
        "node scripts/verify-chromium.js",
        "psql -c 'select 1'",
    ]


@pytest.mark.unit
def test_setup_plan_adds_required_browser_install_when_profile_declares_advisory_duplicate() -> (
    None
):
    """Advisory setup browser-install must not suppress the generated required gate."""
    profile = _profile(
        {
            "runtime": {"browsers": ["chromium"]},
            "phases": {
                "setup": [
                    "npm ci",
                    {"command": "npx playwright install chromium", "required": False},
                ],
            },
        }
    )

    plan = profile_phase_command_plan(profile, ["setup"])

    assert [(step.command.command, step.command.required) for step in plan] == [
        ("npm ci", True),
        ("npx playwright install chromium", False),
        ("npx playwright install chromium", True),
    ]
    assert plan[-1].command.timeout_seconds == _BROWSER_INSTALL_TIMEOUT


@pytest.mark.unit
def test_setup_plan_adds_required_browser_install_when_generated_setup_is_advisory() -> None:
    """Advisory generated_setup browser-install must not suppress the required gate."""
    profile = _profile(
        {
            "runtime": {"browsers": ["chromium"]},
            "phases": {"setup": ["npm ci"]},
            "database": {
                "generated_setup": [
                    {"command": "npx playwright install chromium", "required": False},
                ],
            },
        }
    )

    plan = profile_phase_command_plan(profile, ["setup"])

    assert [(step.command.command, step.command.required) for step in plan] == [
        ("npm ci", True),
        ("npx playwright install chromium", False),
        ("npx playwright install chromium", True),
    ]


@pytest.mark.unit
def test_setup_plan_omits_browser_install_when_already_in_generated_setup() -> None:
    """Generated-setup browser-install is not duplicated in the command plan."""
    profile = _profile(
        {
            "runtime": {"browsers": ["chromium"]},
            "phases": {"setup": ["npm ci"]},
            "database": {"generated_setup": ["npx playwright install chromium"]},
        }
    )

    plan = profile_phase_command_plan(profile, ["setup"])
    commands = [step.command.command for step in plan]

    assert commands == [
        "npm ci",
        "npx playwright install chromium",
    ]
