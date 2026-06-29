"""Pure ``validate``-tool extraction tests (issue #574).

``_leading_executable`` reduces a shell command to its leading PATH-resolvable
executable (or ``None`` when un-probeable), and ``validate_command_probe_targets``
maps a profile's ``validate`` phase to deduped probe targets. Both are pure and
fail-open: an un-probeable leading token is skipped rather than guessed at, so
the downstream probe never false-positives.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.profiles.models import WorkspaceProfile
from awf.runtime.browser_probe import browser_probe_workdir
from awf.runtime.validation_setup import (
    _command_install_trailing_scope_prefix,
    _node_dependency_install_package_manager,
    playwright_browser_install_command,
    profile_phase_command_plan,
)


def _profile_with_validate(commands: list[str]) -> WorkspaceProfile:
    return WorkspaceProfile.model_validate(
        {"name": "validate-profile", "phases": {"validate": commands}}
    )


def _profile_with_setup_and_browsers(commands: list[str]) -> WorkspaceProfile:
    return WorkspaceProfile.model_validate(
        {
            "name": "browser-profile",
            "runtime": {"browsers": ["chromium"]},
            "phases": {"setup": commands},
        }
    )


def _profile_with_setup_validate_and_browsers(
    *,
    setup: list[str],
    validate: list[str],
) -> WorkspaceProfile:
    return WorkspaceProfile.model_validate(
        {
            "name": "browser-profile",
            "runtime": {"browsers": ["chromium"]},
            "phases": {"setup": setup, "validate": validate},
        }
    )


def _profile_with_validate_objects(commands: list[dict[str, object]]) -> WorkspaceProfile:
    return WorkspaceProfile.model_validate(
        {"name": "validate-profile", "phases": {"validate": commands}}
    )


def _profile_with_refresh_and_validate(
    *,
    pre_validation_refresh: list[object],
    validate: list[object],
) -> WorkspaceProfile:
    return WorkspaceProfile.model_validate(
        {
            "name": "validate-profile",
            "phases": {"validate": validate},
            "database": {"pre_validation_refresh": pre_validation_refresh},
        }
    )


def _profile_with_post_agent_and_validate(
    *,
    post_agent: list[object],
    validate: list[object],
    pre_validation_refresh: list[object] | None = None,
) -> WorkspaceProfile:
    payload: dict[str, object] = {
        "name": "validate-profile",
        "phases": {"post_agent": post_agent, "validate": validate},
    }
    if pre_validation_refresh is not None:
        payload["database"] = {"pre_validation_refresh": pre_validation_refresh}
    return WorkspaceProfile.model_validate(payload)


@pytest.mark.unit
class TestPlaywrightBrowserInstallCommand:
    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            ("yarn --immutable", "yarn"),
            ("yarn --cwd apps/web --immutable", "yarn --cwd apps/web"),
            ("yarn --cwd=apps/web --immutable", "yarn --cwd=apps/web"),
            ("yarn --version", None),
            ("yarn --immutable --help", None),
        ],
    )
    def test_dependency_install_parser_distinguishes_yarn_option_only_installs(
        self,
        command: str,
        expected: str | None,
    ) -> None:
        assert _node_dependency_install_package_manager(command) == expected

    @pytest.mark.parametrize(
        ("setup_command", "expected"),
        [
            (
                "pnpm --filter web install",
                "pnpm --filter web exec playwright install chromium",
            ),
            (
                "pnpm -F web install",
                "pnpm -F web exec playwright install chromium",
            ),
        ],
    )
    def test_preserves_pnpm_filter_scope_without_using_as_probe_directory(
        self,
        setup_command: str,
        expected: str,
    ) -> None:
        profile = _profile_with_setup_and_browsers([setup_command])

        command = playwright_browser_install_command(profile)

        assert command is not None
        assert command.command == expected
        assert browser_probe_workdir(profile) == "/workspace"

    def test_root_pnpm_setup_uses_filtered_validate_scope_for_browser_install(self) -> None:
        profile = _profile_with_setup_validate_and_browsers(
            setup=["pnpm install --frozen-lockfile"],
            validate=["pnpm --filter web test:e2e"],
        )

        command = playwright_browser_install_command(profile)

        assert command is not None
        assert command.command == "pnpm --filter web exec playwright install chromium"
        assert browser_probe_workdir(profile) == "/workspace"

    @pytest.mark.parametrize(
        ("validate_command", "expected"),
        [
            ("pnpm -r test:e2e", "pnpm -r exec playwright install chromium"),
            (
                "pnpm --recursive test:e2e",
                "pnpm --recursive exec playwright install chromium",
            ),
        ],
    )
    def test_root_pnpm_setup_preserves_recursive_validate_scope_for_browser_install(
        self,
        validate_command: str,
        expected: str,
    ) -> None:
        profile = _profile_with_setup_validate_and_browsers(
            setup=["pnpm install --frozen-lockfile"],
            validate=[validate_command],
        )

        command = playwright_browser_install_command(profile)

        assert command is not None
        assert command.command == expected
        assert browser_probe_workdir(profile) == "/workspace"

    def test_root_pnpm_setup_does_not_satisfy_filtered_validate_install(self) -> None:
        profile = _profile_with_setup_validate_and_browsers(
            setup=["pnpm install --frozen-lockfile"],
            validate=[
                "pnpm --filter web install --frozen-lockfile",
                "pnpm --filter web test:e2e",
            ],
        )

        commands = profile_phase_command_plan(profile, ["setup", "validate"])

        assert [(command.phase, command.command.command) for command in commands] == [
            ("setup", "pnpm install --frozen-lockfile"),
            ("validate", "pnpm --filter web install --frozen-lockfile"),
            ("setup", "pnpm --filter web exec playwright install chromium"),
            ("validate", "pnpm --filter web test:e2e"),
        ]

    def test_late_scoped_playwright_validate_overrides_earlier_scoped_lint(
        self,
    ) -> None:
        profile = _profile_with_setup_validate_and_browsers(
            setup=["pnpm install --frozen-lockfile"],
            validate=[
                "pnpm --filter api lint",
                "pnpm --filter web exec playwright test",
            ],
        )

        command = playwright_browser_install_command(profile)

        assert command is not None
        assert command.command == "pnpm --filter web exec playwright install chromium"
        assert browser_probe_workdir(profile) == "/workspace"

    def test_late_scoped_browser_script_validate_overrides_earlier_scoped_lint(
        self,
    ) -> None:
        profile = _profile_with_setup_validate_and_browsers(
            setup=["pnpm install --frozen-lockfile"],
            validate=[
                "pnpm --filter api lint",
                "pnpm --filter web test:e2e",
            ],
        )

        command = playwright_browser_install_command(profile)

        assert command is not None
        assert command.command == "pnpm --filter web exec playwright install chromium"
        assert browser_probe_workdir(profile) == "/workspace"

    def test_late_scoped_suffixed_e2e_script_validate_overrides_earlier_scoped_lint(
        self,
    ) -> None:
        profile = _profile_with_setup_validate_and_browsers(
            setup=["pnpm install --frozen-lockfile"],
            validate=[
                "pnpm --filter api lint",
                "pnpm --filter web test:e2e:ci",
            ],
        )

        command = playwright_browser_install_command(profile)

        assert command is not None
        assert command.command == "pnpm --filter web exec playwright install chromium"
        assert browser_probe_workdir(profile) == "/workspace"

    def test_late_yarn_workspace_run_browser_script_overrides_earlier_scoped_lint(
        self,
    ) -> None:
        profile = _profile_with_setup_validate_and_browsers(
            setup=["yarn install --immutable"],
            validate=[
                "yarn workspace api run lint",
                "yarn workspace web run test:e2e",
            ],
        )

        command = playwright_browser_install_command(profile)

        assert command is not None
        assert command.command == "yarn workspace web playwright install chromium"
        assert browser_probe_workdir(profile) == "/workspace"

    def test_late_browser_validate_scope_overrides_earlier_scoped_setup_install(
        self,
    ) -> None:
        profile = _profile_with_setup_validate_and_browsers(
            setup=["pnpm --filter api install", "pnpm --filter web install"],
            validate=["pnpm --filter web test:e2e"],
        )

        command = playwright_browser_install_command(profile)

        assert command is not None
        assert command.command == "pnpm --filter web exec playwright install chromium"
        assert browser_probe_workdir(profile) == "/workspace"

    def test_root_pnpm_setup_scans_past_corepack_validate_preamble_for_scope(self) -> None:
        profile = _profile_with_setup_validate_and_browsers(
            setup=["pnpm install --frozen-lockfile"],
            validate=["corepack enable && pnpm --filter web test:e2e"],
        )

        command = playwright_browser_install_command(profile)

        assert command is not None
        assert command.command == "pnpm --filter web exec playwright install chromium"
        assert browser_probe_workdir(profile) == "/workspace"

    def test_root_pnpm_setup_uses_cd_scoped_validate_directory_for_browser_install(
        self,
    ) -> None:
        profile = _profile_with_setup_validate_and_browsers(
            setup=["pnpm install --frozen-lockfile"],
            validate=["cd apps/web && pnpm run e2e"],
        )

        command = playwright_browser_install_command(profile)

        assert command is not None
        assert command.command == "pnpm -C apps/web exec playwright install chromium"
        assert browser_probe_workdir(profile) == "/workspace/apps/web"

    @pytest.mark.parametrize(
        ("validate_command", "expected"),
        [
            (
                "npm run test:e2e --workspace web",
                "npm --workspace web exec -- playwright install chromium",
            ),
            (
                "npm test -w web",
                "npm -w web exec -- playwright install chromium",
            ),
            (
                "npm exec --workspace web -- playwright test",
                "npm --workspace web exec -- playwright install chromium",
            ),
            (
                "npm x -w web -- playwright test",
                "npm -w web exec -- playwright install chromium",
            ),
        ],
    )
    def test_root_npm_setup_uses_late_validate_workspace_scope_for_browser_install(
        self,
        validate_command: str,
        expected: str,
    ) -> None:
        profile = _profile_with_setup_validate_and_browsers(
            setup=["npm ci"],
            validate=[validate_command],
        )

        command = playwright_browser_install_command(profile)

        assert command is not None
        assert command.command == expected
        assert browser_probe_workdir(profile) == "/workspace"

    def test_validate_playwright_command_infers_unscoped_package_manager(self) -> None:
        profile = _profile_with_setup_validate_and_browsers(
            setup=[],
            validate=["yarn playwright test"],
        )

        command = playwright_browser_install_command(profile)

        assert command is not None
        assert command.command == "yarn playwright install chromium"
        assert browser_probe_workdir(profile) == "/workspace"

    def test_validate_browser_install_preserves_pre_install_validate_order(self) -> None:
        profile = _profile_with_setup_validate_and_browsers(
            setup=[],
            validate=[
                "node scripts/write-npmrc.js",
                "pnpm install",
                "pnpm test",
            ],
        )

        commands = profile_phase_command_plan(profile, ["validate"])

        assert [(command.phase, command.command.command) for command in commands] == [
            ("validate", "node scripts/write-npmrc.js"),
            ("validate", "pnpm install"),
            ("setup", "pnpm exec playwright install chromium"),
            ("validate", "pnpm test"),
        ]

    def test_validate_python_browser_install_waits_for_pip_install(self) -> None:
        profile = _profile_with_setup_validate_and_browsers(
            setup=[],
            validate=[
                "python -m pip install playwright",
                "pytest --browser chromium",
            ],
        )

        commands = profile_phase_command_plan(profile, ["validate"])

        assert [(command.phase, command.command.command) for command in commands] == [
            ("validate", "python -m pip install playwright"),
            ("setup", "python -m playwright install chromium"),
            ("validate", "pytest --browser chromium"),
        ]
        assert commands[1].command.required is False

    @pytest.mark.parametrize(
        ("setup_command", "expected_install"),
        [
            (
                "uv add --project apps/web playwright",
                "uv run --project apps/web -m playwright install chromium",
            ),
            (
                "uv add --project=apps/web playwright",
                "uv run --project apps/web -m playwright install chromium",
            ),
            (
                "uv add --directory apps/web playwright",
                "uv run --directory apps/web -m playwright install chromium",
            ),
            (
                "uv add --directory=apps/web playwright",
                "uv run --directory apps/web -m playwright install chromium",
            ),
            (
                "uv add --package web playwright",
                "uv run --package web -m playwright install chromium",
            ),
            (
                "uv add --package=web playwright",
                "uv run --package web -m playwright install chromium",
            ),
        ],
    )
    def test_uv_add_scope_uses_python_playwright(
        self,
        tmp_path: Path,
        setup_command: str,
        expected_install: str,
    ) -> None:
        workspace_root = tmp_path / "workspace"
        project_root = workspace_root / "apps" / "web"
        project_root.mkdir(parents=True)
        (workspace_root / "pyproject.toml").write_text(
            "\n".join(
                [
                    "[project]",
                    'name = "workspace"',
                    'version = "0.1.0"',
                    "[tool.uv.workspace]",
                    'members = ["apps/*"]',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        (project_root / "pyproject.toml").write_text(
            "\n".join(
                [
                    "[project]",
                    'name = "web"',
                    'version = "0.1.0"',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        profile = _profile_with_setup_validate_and_browsers(
            setup=[setup_command],
            validate=["uv run --project apps/web pytest --browser chromium"],
        )

        command = playwright_browser_install_command(profile, workspace_root=workspace_root)

        assert command is not None
        assert command.command == expected_install

    @pytest.mark.parametrize(
        ("setup_command", "project_dir", "expected_install"),
        [
            (
                "uv sync --extra e2e",
                "",
                "uv run --extra e2e -m playwright install chromium",
            ),
            (
                "cd apps/web && uv sync --extra e2e",
                "apps/web",
                "cd apps/web && uv run --extra e2e -m playwright install chromium",
            ),
            (
                "uv sync --project apps/web --group e2e",
                "apps/web",
                "uv run --project apps/web --group e2e -m playwright install chromium",
            ),
            (
                "uv sync --project=apps/web --extra=e2e",
                "apps/web",
                "uv run --project apps/web --extra e2e -m playwright install chromium",
            ),
            (
                "uv sync --project /workspace/apps/web --group e2e",
                "apps/web",
                "uv run --project /workspace/apps/web --group e2e -m playwright install chromium",
            ),
            (
                "uv sync --project=/workspace/apps/web --extra=e2e",
                "apps/web",
                "uv run --project /workspace/apps/web --extra e2e -m playwright install chromium",
            ),
            (
                "uv sync --directory apps/web --group e2e",
                "apps/web",
                "uv run --directory apps/web --group e2e -m playwright install chromium",
            ),
            (
                "uv sync --directory=apps/web --extra=e2e",
                "apps/web",
                "uv run --directory apps/web --extra e2e -m playwright install chromium",
            ),
            (
                "uv sync --directory /workspace/apps/web --group e2e",
                "apps/web",
                "uv run --directory /workspace/apps/web --group e2e -m playwright install chromium",
            ),
            (
                "uv sync --directory=/workspace/apps/web --extra=e2e",
                "apps/web",
                "uv run --directory /workspace/apps/web --extra e2e -m playwright install chromium",
            ),
        ],
    )
    def test_uv_sync_pyproject_scope_uses_python_playwright(
        self,
        tmp_path: Path,
        setup_command: str,
        project_dir: str,
        expected_install: str,
    ) -> None:
        workspace_root = tmp_path / "workspace"
        project_root = workspace_root / project_dir
        project_root.mkdir(parents=True)
        (project_root / "pyproject.toml").write_text(
            "\n".join(
                [
                    "[project]",
                    'name = "browser-profile"',
                    'version = "0.1.0"',
                    "[project.optional-dependencies]",
                    'e2e = ["pytest-playwright"]',
                    "[dependency-groups]",
                    'e2e = ["pytest-playwright"]',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        profile = _profile_with_setup_validate_and_browsers(
            setup=[setup_command],
            validate=["uv run pytest --browser chromium"],
        )

        command = playwright_browser_install_command(profile, workspace_root=workspace_root)

        assert command is not None
        assert command.command == expected_install

    @pytest.mark.parametrize(
        ("setup_command", "expected_install"),
        [
            (
                "uv sync --package web --group e2e",
                "uv run --package web --group e2e -m playwright install chromium",
            ),
            (
                "uv sync --package=web --extra=e2e",
                "uv run --package web --extra e2e -m playwright install chromium",
            ),
        ],
    )
    def test_uv_sync_package_scope_uses_workspace_member_python_playwright(
        self,
        tmp_path: Path,
        setup_command: str,
        expected_install: str,
    ) -> None:
        workspace_root = tmp_path / "workspace"
        project_root = workspace_root / "apps" / "web"
        project_root.mkdir(parents=True)
        (workspace_root / "pyproject.toml").write_text(
            "\n".join(
                [
                    "[project]",
                    'name = "root"',
                    'version = "0.1.0"',
                    "[tool.uv.workspace]",
                    'members = ["apps/*"]',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        (project_root / "pyproject.toml").write_text(
            "\n".join(
                [
                    "[project]",
                    'name = "web"',
                    'version = "0.1.0"',
                    "[project.optional-dependencies]",
                    'e2e = ["pytest-playwright"]',
                    "[dependency-groups]",
                    'e2e = ["pytest-playwright"]',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        profile = _profile_with_setup_validate_and_browsers(
            setup=[setup_command],
            validate=["uv run --package web pytest --browser chromium"],
        )

        command = playwright_browser_install_command(profile, workspace_root=workspace_root)

        assert command is not None
        assert command.command == expected_install

    @pytest.mark.parametrize(
        ("setup_command", "expected_install"),
        [
            ("uv sync --group e2e", "uv run --group e2e -m playwright install chromium"),
            ("uv sync --group=e2e", "uv run --group e2e -m playwright install chromium"),
            ("uv sync --all-groups", "uv run --all-groups -m playwright install chromium"),
        ],
    )
    def test_uv_sync_dependency_group_uses_python_playwright(
        self,
        tmp_path: Path,
        setup_command: str,
        expected_install: str,
    ) -> None:
        workspace_root = tmp_path / "workspace"
        workspace_root.mkdir()
        (workspace_root / "pyproject.toml").write_text(
            "\n".join(
                [
                    "[project]",
                    'name = "browser-profile"',
                    'version = "0.1.0"',
                    "[dependency-groups]",
                    'docs = ["sphinx"]',
                    'e2e = ["pytest-playwright"]',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        profile = _profile_with_setup_validate_and_browsers(
            setup=[setup_command],
            validate=["uv run pytest --browser chromium"],
        )

        command = playwright_browser_install_command(profile, workspace_root=workspace_root)

        assert command is not None
        assert command.command == expected_install

    def test_uv_sync_dependency_group_include_uses_python_playwright(
        self,
        tmp_path: Path,
    ) -> None:
        workspace_root = tmp_path / "workspace"
        workspace_root.mkdir()
        (workspace_root / "pyproject.toml").write_text(
            "\n".join(
                [
                    "[project]",
                    'name = "browser-profile"',
                    'version = "0.1.0"',
                    "[dependency-groups]",
                    'dev = [{include-group = "e2e"}]',
                    'e2e = ["pytest-playwright"]',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        profile = _profile_with_setup_validate_and_browsers(
            setup=["uv sync --group dev"],
            validate=["uv run pytest --browser chromium"],
        )

        command = playwright_browser_install_command(profile, workspace_root=workspace_root)

        assert command is not None
        assert command.command == "uv run --group dev -m playwright install chromium"

    @pytest.mark.parametrize(
        ("pyproject_lines", "setup_command"),
        [
            (
                [
                    "[dependency-groups]",
                    'dev = ["pytest-playwright"]',
                ],
                "uv sync",
            ),
            (
                [
                    "[tool.uv]",
                    'default-groups = ["e2e"]',
                    "[dependency-groups]",
                    'dev = ["sphinx"]',
                    'e2e = ["pytest-playwright"]',
                ],
                "uv sync",
            ),
            (
                [
                    "[tool.uv]",
                    'default-groups = "all"',
                    "[dependency-groups]",
                    'docs = ["sphinx"]',
                    'e2e = ["pytest-playwright"]',
                ],
                "uv sync",
            ),
        ],
    )
    def test_uv_sync_default_dependency_group_uses_python_playwright(
        self,
        tmp_path: Path,
        pyproject_lines: list[str],
        setup_command: str,
    ) -> None:
        workspace_root = tmp_path / "workspace"
        workspace_root.mkdir()
        (workspace_root / "pyproject.toml").write_text(
            "\n".join(
                [
                    "[project]",
                    'name = "browser-profile"',
                    'version = "0.1.0"',
                    *pyproject_lines,
                    "",
                ]
            ),
            encoding="utf-8",
        )
        profile = _profile_with_setup_validate_and_browsers(
            setup=[setup_command],
            validate=["uv run pytest --browser chromium"],
        )

        command = playwright_browser_install_command(profile, workspace_root=workspace_root)

        assert command is not None
        assert command.command == "uv run -m playwright install chromium"

    @pytest.mark.parametrize(
        "setup_command",
        [
            "uv sync --no-default-groups",
            "uv sync --no-dev",
            "uv sync --no-group dev",
            "uv sync --no-group=dev",
        ],
    )
    def test_uv_sync_default_dependency_group_opt_out_uses_node_playwright(
        self,
        tmp_path: Path,
        setup_command: str,
    ) -> None:
        workspace_root = tmp_path / "workspace"
        workspace_root.mkdir()
        (workspace_root / "pyproject.toml").write_text(
            "\n".join(
                [
                    "[project]",
                    'name = "browser-profile"',
                    'version = "0.1.0"',
                    "[dependency-groups]",
                    'dev = ["pytest-playwright"]',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        profile = _profile_with_setup_validate_and_browsers(
            setup=[setup_command],
            validate=["uv run pytest --browser chromium"],
        )

        command = playwright_browser_install_command(profile, workspace_root=workspace_root)

        assert command is not None
        assert command.command == "npx playwright install chromium"

    def test_uv_sync_pyproject_dependency_uses_python_playwright(
        self,
        tmp_path: Path,
    ) -> None:
        workspace_root = tmp_path / "workspace"
        workspace_root.mkdir()
        (workspace_root / "pyproject.toml").write_text(
            "\n".join(
                [
                    "[project]",
                    'name = "browser-profile"',
                    'version = "0.1.0"',
                    'dependencies = ["pytest-playwright >= 0.5"]',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        profile = _profile_with_setup_validate_and_browsers(
            setup=["uv sync"],
            validate=["uv run pytest --browser chromium"],
        )

        commands = profile_phase_command_plan(
            profile,
            ["setup", "validate"],
            workspace_root=workspace_root,
        )

        assert [(item.phase, item.command.command) for item in commands] == [
            ("setup", "uv sync"),
            ("setup", "uv run -m playwright install chromium"),
            ("validate", "uv run pytest --browser chromium"),
        ]
        assert commands[1].command.required is False

    def test_validate_python_browser_install_waits_for_path_qualified_pip_install(
        self,
    ) -> None:
        profile = _profile_with_setup_validate_and_browsers(
            setup=[],
            validate=[
                ".venv/bin/pip install playwright",
                ".venv/bin/pytest --browser chromium",
            ],
        )

        commands = profile_phase_command_plan(profile, ["validate"])

        assert [(command.phase, command.command.command) for command in commands] == [
            ("validate", ".venv/bin/pip install playwright"),
            ("setup", ".venv/bin/python -m playwright install chromium"),
            ("validate", ".venv/bin/pytest --browser chromium"),
        ]
        assert commands[1].command.required is False

    def test_validate_python_playwright_dependency_install_not_inserted_before_pre_agent(
        self,
    ) -> None:
        profile = WorkspaceProfile.model_validate(
            {
                "name": "browser-profile",
                "runtime": {"browsers": ["chromium"]},
                "phases": {
                    "pre_agent": ["pytest --browser chromium"],
                    "validate": [
                        "python -m pip install pytest-playwright",
                        "pytest --browser chromium",
                    ],
                },
            }
        )

        pre_agent_commands = profile_phase_command_plan(profile, ["setup", "pre_agent"])
        validate_commands = profile_phase_command_plan(profile, ["validate"])

        assert [(command.phase, command.command.command) for command in pre_agent_commands] == [
            ("pre_agent", "pytest --browser chromium"),
        ]
        assert [(command.phase, command.command.command) for command in validate_commands] == [
            ("validate", "python -m pip install pytest-playwright"),
            ("setup", "python -m playwright install chromium"),
            ("validate", "pytest --browser chromium"),
        ]
        assert validate_commands[1].command.required is False

    def test_scoped_validate_python_requirement_install_uses_cd_directory(
        self,
        tmp_path: Path,
    ) -> None:
        workspace_root = tmp_path / "workspace"
        app_root = workspace_root / "apps" / "web"
        app_root.mkdir(parents=True)
        (app_root / "requirements.txt").write_text("playwright\n", encoding="utf-8")
        profile = _profile_with_setup_validate_and_browsers(
            setup=[],
            validate=["cd apps/web && python -m pip install -r requirements.txt"],
        )

        command = playwright_browser_install_command(profile, workspace_root=workspace_root)

        assert command is not None
        assert command.command == "cd apps/web && python -m playwright install chromium"

    def test_validate_python_editable_requirement_file_uses_python_playwright(
        self,
        tmp_path: Path,
    ) -> None:
        workspace_root = tmp_path / "workspace"
        workspace_root.mkdir()
        (workspace_root / "requirements.txt").write_text("-e .[e2e]\n", encoding="utf-8")
        (workspace_root / "pyproject.toml").write_text(
            "\n".join(
                [
                    "[project]",
                    'name = "browser-profile"',
                    'version = "0.1.0"',
                    "[project.optional-dependencies]",
                    'e2e = ["pytest-playwright"]',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        profile = _profile_with_setup_validate_and_browsers(
            setup=[],
            validate=["python -m pip install -r requirements.txt"],
        )

        command = playwright_browser_install_command(profile, workspace_root=workspace_root)

        assert command is not None
        assert command.command == "python -m playwright install chromium"

    def test_validate_python_browser_install_splits_direct_install_and_test_chain(self) -> None:
        profile = _profile_with_setup_validate_and_browsers(
            setup=[],
            validate=["python -m pip install playwright && pytest --browser chromium"],
        )

        commands = profile_phase_command_plan(profile, ["validate"])

        assert [(command.phase, command.command.command) for command in commands] == [
            ("validate", "python -m pip install playwright"),
            ("setup", "python -m playwright install chromium"),
            ("validate", "pytest --browser chromium"),
        ]
        assert commands[1].command.required is False

    def test_validate_browser_install_splits_direct_install_and_test_chain(self) -> None:
        profile = _profile_with_setup_validate_and_browsers(
            setup=[],
            validate=["pnpm install --frozen-lockfile && pnpm test:e2e"],
        )

        commands = profile_phase_command_plan(profile, ["validate"])

        assert [(command.phase, command.command.command) for command in commands] == [
            ("validate", "pnpm install --frozen-lockfile"),
            ("setup", "pnpm exec playwright install chromium"),
            ("validate", "pnpm test:e2e"),
        ]
        assert commands[1].command.required is False

    def test_validate_browser_install_preserves_npm_workspaces_install_scope(
        self,
    ) -> None:
        profile = _profile_with_setup_validate_and_browsers(
            setup=[],
            validate=[
                "npm install --workspaces && npm --workspace web exec playwright test",
            ],
        )

        commands = profile_phase_command_plan(profile, ["validate"])

        assert [(command.phase, command.command.command) for command in commands] == [
            ("validate", "npm install --workspaces"),
            ("setup", "npm --workspace web exec -- playwright install chromium"),
            ("validate", "npm --workspace web exec playwright test"),
        ]
        assert commands[1].command.required is False

    @pytest.mark.parametrize("phase", ["setup", "validate"])
    def test_browser_install_splits_assignment_only_install_preamble(
        self,
        phase: str,
    ) -> None:
        profile = WorkspaceProfile.model_validate(
            {
                "name": "browser-profile",
                "runtime": {"browsers": ["chromium"]},
                "phases": {
                    phase: ["PATH=/opt/node/bin:$PATH; pnpm install; pnpm exec playwright test"],
                },
            }
        )

        commands = profile_phase_command_plan(profile, [phase])

        assert [(command.phase, command.command.command) for command in commands] == [
            (phase, "PATH=/opt/node/bin:$PATH; pnpm install"),
            ("setup", "PATH=/opt/node/bin:$PATH && pnpm exec playwright install chromium"),
            (phase, "PATH=/opt/node/bin:$PATH && pnpm exec playwright test"),
        ]
        assert commands[1].command.required is False

    def test_browser_install_replays_inline_install_assignments_for_browser_install(
        self,
    ) -> None:
        profile = _profile_with_setup_validate_and_browsers(
            setup=[],
            validate=[
                "PATH=/opt/node/bin:$PATH PLAYWRIGHT_BROWSERS_PATH=.pw "
                "pnpm install && PATH=/opt/node/bin:$PATH "
                "PLAYWRIGHT_BROWSERS_PATH=.pw pnpm exec playwright test"
            ],
        )

        commands = profile_phase_command_plan(profile, ["validate"])

        assert [(command.phase, command.command.command) for command in commands] == [
            (
                "validate",
                "PATH=/opt/node/bin:$PATH PLAYWRIGHT_BROWSERS_PATH=.pw pnpm install",
            ),
            (
                "setup",
                "export PATH=/opt/node/bin:$PATH PLAYWRIGHT_BROWSERS_PATH=.pw "
                "&& pnpm exec playwright install chromium",
            ),
            (
                "validate",
                "PATH=/opt/node/bin:$PATH PLAYWRIGHT_BROWSERS_PATH=.pw pnpm exec playwright test",
            ),
        ]
        assert commands[1].command.required is False

    def test_browser_install_does_not_replay_unsafe_inline_install_assignment(
        self,
    ) -> None:
        profile = _profile_with_setup_validate_and_browsers(
            setup=[],
            validate=[
                "PLAYWRIGHT_BROWSERS_PATH=`pwd` pnpm install && "
                "PLAYWRIGHT_BROWSERS_PATH=`pwd` pnpm exec playwright test"
            ],
        )

        commands = profile_phase_command_plan(profile, ["validate"])

        assert [(command.phase, command.command.command) for command in commands] == [
            ("validate", "PLAYWRIGHT_BROWSERS_PATH=`pwd` pnpm install"),
            ("setup", "pnpm exec playwright install chromium"),
            ("validate", "PLAYWRIGHT_BROWSERS_PATH=`pwd` pnpm exec playwright test"),
        ]
        assert commands[1].command.required is False

    def test_validate_browser_install_ignores_separators_inside_shell_comment(
        self,
    ) -> None:
        profile = _profile_with_setup_validate_and_browsers(
            setup=[],
            validate=["pnpm install --frozen-lockfile # keep; note\npnpm test:e2e"],
        )

        commands = profile_phase_command_plan(profile, ["validate"])

        assert [(command.phase, command.command.command) for command in commands] == [
            ("validate", "pnpm install --frozen-lockfile # keep; note"),
            ("setup", "pnpm exec playwright install chromium"),
            ("validate", "pnpm test:e2e"),
        ]
        assert commands[1].command.required is False

    def test_validate_browser_install_split_replays_exported_state_for_trailing_command(
        self,
    ) -> None:
        profile = _profile_with_setup_validate_and_browsers(
            setup=[],
            validate=[
                "export PLAYWRIGHT_BROWSERS_PATH=.pw; pnpm install; pnpm exec playwright test"
            ],
        )

        commands = profile_phase_command_plan(profile, ["validate"])

        assert [(command.phase, command.command.command) for command in commands] == [
            ("validate", "export PLAYWRIGHT_BROWSERS_PATH=.pw; pnpm install"),
            (
                "setup",
                "export PLAYWRIGHT_BROWSERS_PATH=.pw && pnpm exec playwright install chromium",
            ),
            (
                "validate",
                "export PLAYWRIGHT_BROWSERS_PATH=.pw && pnpm exec playwright test",
            ),
        ]
        assert commands[1].command.required is False

    def test_validate_browser_install_does_not_split_unpreserved_exported_state(
        self,
    ) -> None:
        validate_command = "export BASE_URL; pnpm install; pnpm test:e2e"
        profile = _profile_with_setup_validate_and_browsers(
            setup=[],
            validate=[validate_command],
        )

        commands = profile_phase_command_plan(profile, ["validate"])

        assert [(command.phase, command.command.command) for command in commands] == [
            ("validate", validate_command),
            ("setup", "pnpm exec playwright install chromium"),
        ]
        assert commands[1].command.required is False

    def test_validate_browser_install_does_not_split_variable_expanded_export(
        self,
    ) -> None:
        validate_command = "export BASE_URL=$HOST; pnpm install; pnpm test:e2e"
        profile = _profile_with_setup_validate_and_browsers(
            setup=[],
            validate=[validate_command],
        )

        commands = profile_phase_command_plan(profile, ["validate"])

        assert [(command.phase, command.command.command) for command in commands] == [
            ("validate", validate_command),
            ("setup", "pnpm exec playwright install chromium"),
        ]
        assert commands[1].command.required is False

    def test_validate_browser_install_does_not_split_unsafe_assignment_only_state(
        self,
    ) -> None:
        validate_command = "PATH=`node-bin`; pnpm install; pnpm test:e2e"
        profile = _profile_with_setup_validate_and_browsers(
            setup=[],
            validate=[validate_command],
        )

        commands = profile_phase_command_plan(profile, ["validate"])

        assert [(command.phase, command.command.command) for command in commands] == [
            ("validate", validate_command),
            ("setup", "pnpm exec playwright install chromium"),
        ]
        assert commands[1].command.required is False

    def test_browser_install_split_does_not_replay_command_substitution_assignment(
        self,
    ) -> None:
        assert _command_install_trailing_scope_prefix("PATH=$(node-bin)") is None

    @pytest.mark.parametrize("source_prefix", ["source .env", ". .env"])
    def test_validate_browser_install_does_not_split_sourced_shell_state(
        self,
        source_prefix: str,
    ) -> None:
        validate_command = f"{source_prefix}; pnpm install; pnpm test:e2e"
        profile = _profile_with_setup_validate_and_browsers(
            setup=[],
            validate=[validate_command],
        )

        commands = profile_phase_command_plan(profile, ["validate"])

        assert [(command.phase, command.command.command) for command in commands] == [
            ("validate", validate_command),
            ("setup", "pnpm exec playwright install chromium"),
        ]
        assert commands[1].command.required is False

    def test_validate_browser_install_does_not_split_eval_preamble_state(
        self,
    ) -> None:
        validate_command = 'eval "$(mise activate bash)"; pnpm install; pnpm exec playwright test'
        profile = _profile_with_setup_validate_and_browsers(
            setup=[],
            validate=[validate_command],
        )

        commands = profile_phase_command_plan(profile, ["validate"])

        assert [(command.phase, command.command.command) for command in commands] == [
            ("validate", validate_command),
            ("setup", "pnpm exec playwright install chromium"),
        ]
        assert commands[1].command.required is False

    def test_validate_browser_install_does_not_split_toolchain_switcher_state(
        self,
    ) -> None:
        validate_command = "nvm use 20; pnpm install; pnpm exec playwright test"
        profile = _profile_with_setup_validate_and_browsers(
            setup=[],
            validate=[validate_command],
        )

        commands = profile_phase_command_plan(profile, ["validate"])

        assert [(command.phase, command.command.command) for command in commands] == [
            ("validate", validate_command),
            ("setup", "pnpm exec playwright install chromium"),
        ]
        assert commands[1].command.required is False

    def test_validate_browser_install_split_preserves_cd_scope_for_trailing_command(
        self,
    ) -> None:
        profile = _profile_with_setup_validate_and_browsers(
            setup=[],
            validate=["cd apps/web && pnpm install && pnpm test:e2e"],
        )

        commands = profile_phase_command_plan(profile, ["validate"])

        assert [(command.phase, command.command.command) for command in commands] == [
            ("validate", "cd apps/web && pnpm install"),
            ("setup", "pnpm -C apps/web exec playwright install chromium"),
            ("validate", "cd apps/web && pnpm test:e2e"),
        ]
        assert commands[1].command.required is False

    def test_validate_browser_install_does_not_split_shell_expanded_cd_scope(
        self,
    ) -> None:
        validate_command = 'cd "$APP_DIR"; pnpm install; pnpm exec playwright test'
        profile = _profile_with_setup_validate_and_browsers(
            setup=[],
            validate=[validate_command],
        )

        commands = profile_phase_command_plan(profile, ["validate"])

        assert [(command.phase, command.command.command) for command in commands] == [
            ("validate", validate_command),
            ("setup", "pnpm exec playwright install chromium"),
        ]
        assert commands[1].command.required is False

    def test_validate_browser_install_split_preserves_guarded_cd_scope_for_trailing_command(
        self,
    ) -> None:
        profile = _profile_with_setup_validate_and_browsers(
            setup=[],
            validate=["set -e; cd apps/web; pnpm install; pnpm test:e2e"],
        )

        commands = profile_phase_command_plan(profile, ["validate"])

        assert [(command.phase, command.command.command) for command in commands] == [
            ("validate", "set -e; cd apps/web; pnpm install"),
            ("setup", "pnpm -C apps/web exec playwright install chromium"),
            ("validate", "set -e; cd apps/web && pnpm test:e2e"),
        ]
        assert commands[1].command.required is False

    def test_validate_browser_install_split_preserves_guard_for_trailing_command(
        self,
    ) -> None:
        profile = _profile_with_setup_validate_and_browsers(
            setup=[],
            validate=["set -e; pnpm install; npm run build; pnpm test:e2e"],
        )

        commands = profile_phase_command_plan(profile, ["validate"])

        assert [(command.phase, command.command.command) for command in commands] == [
            ("validate", "set -e; pnpm install"),
            ("setup", "pnpm exec playwright install chromium"),
            ("validate", "set -e && npm run build; pnpm test:e2e"),
        ]
        assert commands[1].command.required is False

    def test_validate_browser_install_split_treats_package_add_as_install(
        self,
    ) -> None:
        profile = _profile_with_setup_validate_and_browsers(
            setup=[],
            validate=["pnpm add @playwright/test && pnpm exec playwright test"],
        )

        commands = profile_phase_command_plan(profile, ["validate"])

        assert [(command.phase, command.command.command) for command in commands] == [
            ("validate", "pnpm add @playwright/test"),
            ("setup", "pnpm exec playwright install chromium"),
            ("validate", "pnpm exec playwright test"),
        ]
        assert commands[1].command.required is False

    def test_validate_browser_install_splits_after_later_matching_chained_install(
        self,
    ) -> None:
        profile = _profile_with_setup_validate_and_browsers(
            setup=[],
            validate=[
                "pnpm -C docs install && pnpm -C web install && pnpm -C web exec playwright test"
            ],
        )

        commands = profile_phase_command_plan(profile, ["validate"])

        assert [(command.phase, command.command.command) for command in commands] == [
            ("validate", "pnpm -C docs install && pnpm -C web install"),
            ("setup", "pnpm -C web exec playwright install chromium"),
            ("validate", "pnpm -C web exec playwright test"),
        ]
        assert commands[1].command.required is False

    def test_pnpm_workspace_root_install_gets_browser_install_before_pre_agent(
        self,
    ) -> None:
        profile = WorkspaceProfile.model_validate(
            {
                "name": "browser-profile",
                "runtime": {"browsers": ["chromium"]},
                "phases": {
                    "setup": ["pnpm -w install"],
                    "pre_agent": ["pnpm exec playwright test"],
                },
            }
        )

        commands = profile_phase_command_plan(profile, ["setup", "pre_agent"])

        assert [(command.phase, command.command.command) for command in commands] == [
            ("setup", "pnpm -w install"),
            ("setup", "pnpm exec playwright install chromium"),
            ("pre_agent", "pnpm exec playwright test"),
        ]

    def test_post_agent_playwright_dependency_install_does_not_defer_pre_agent_usage(
        self,
    ) -> None:
        profile = WorkspaceProfile.model_validate(
            {
                "name": "browser-profile",
                "runtime": {"browsers": ["chromium"]},
                "phases": {
                    "pre_agent": ["pnpm exec playwright test"],
                    "post_agent": ["pnpm install"],
                },
            }
        )

        commands = profile_phase_command_plan(profile, ["setup", "pre_agent"])

        assert [(command.phase, command.command.command) for command in commands] == [
            ("setup", "pnpm exec playwright install chromium"),
            ("pre_agent", "pnpm exec playwright test"),
        ]

    def test_post_agent_python_playwright_dependency_install_does_not_defer_pre_agent_usage(
        self,
    ) -> None:
        profile = WorkspaceProfile.model_validate(
            {
                "name": "browser-profile",
                "runtime": {"browsers": ["chromium"]},
                "phases": {
                    "pre_agent": ["python -m playwright --version"],
                    "post_agent": [
                        "python -m pip install playwright",
                        "python -m playwright test",
                    ],
                },
            }
        )

        pre_agent_commands = profile_phase_command_plan(profile, ["setup", "pre_agent"])
        post_agent_commands = profile_phase_command_plan(profile, ["post_agent"])

        assert [(command.phase, command.command.command) for command in pre_agent_commands] == [
            ("setup", "python -m playwright install chromium"),
            ("pre_agent", "python -m playwright --version"),
        ]
        assert [(command.phase, command.command.command) for command in post_agent_commands] == [
            ("post_agent", "python -m pip install playwright"),
            ("setup", "python -m playwright install chromium"),
            ("post_agent", "python -m playwright test"),
        ]

    def test_yarn_version_setup_probe_defers_browser_install_until_validate_install(
        self,
    ) -> None:
        profile = _profile_with_setup_validate_and_browsers(
            setup=["yarn --version"],
            validate=["yarn install", "yarn test"],
        )

        commands = profile_phase_command_plan(profile, ["setup", "validate"])

        assert [(command.phase, command.command.command) for command in commands] == [
            ("setup", "yarn --version"),
            ("validate", "yarn install"),
            ("setup", "yarn playwright install chromium"),
            ("validate", "yarn test"),
        ]

    def test_post_agent_preserves_validate_deferred_browser_install(
        self,
    ) -> None:
        profile = WorkspaceProfile.model_validate(
            {
                "name": "browser-profile",
                "runtime": {"browsers": ["chromium"]},
                "phases": {
                    "setup": ["node --version"],
                    "post_agent": ["echo post-agent"],
                    "validate": ["pnpm install", "pnpm test"],
                },
            }
        )

        commands = profile_phase_command_plan(profile, ["setup", "post_agent", "validate"])

        assert [(command.phase, command.command.command) for command in commands] == [
            ("setup", "node --version"),
            ("post_agent", "echo post-agent"),
            ("validate", "pnpm install"),
            ("setup", "pnpm exec playwright install chromium"),
            ("validate", "pnpm test"),
        ]
