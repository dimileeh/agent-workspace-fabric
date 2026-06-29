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
from awf.runtime.validation import (
    _leading_executable,
    _leading_executables,
    validate_command_probe_targets,
)
from awf.runtime.validation_setup import (
    _command_install_trailing_scope_prefix,
    _node_dependency_install_package_manager,
    _node_package_manager_package_dir,
    playwright_browser_install_command,
    playwright_command,
    profile_phase_command_plan,
    runtime_browser_probe_deferred_until_validate,
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
        ("package_manager", "expected"),
        [
            ("yarn --cwd apps/web", "yarn --cwd apps/web playwright test"),
            ("bun --cwd apps/web", "cd apps/web && bunx playwright test"),
            ("bun --cwd=apps/web", "cd apps/web && bunx playwright test"),
            ('npm "unterminated', "npx playwright test"),
        ],
    )
    def test_playwright_command_handles_scoped_and_unparseable_package_managers(
        self,
        package_manager: str,
        expected: str,
    ) -> None:
        assert playwright_command(package_manager, "test") == expected

    @pytest.mark.parametrize(
        ("setup_command", "expected"),
        [
            (
                "npm --prefix apps/web ci",
                "npm --prefix apps/web exec -- playwright install chromium",
            ),
            (
                "npm --cwd apps/web install",
                "npm --cwd apps/web exec -- playwright install chromium",
            ),
            (
                "npm ci --prefix apps/web",
                "npm --prefix apps/web exec -- playwright install chromium",
            ),
            (
                "npm install --prefix=apps/web",
                "npm --prefix=apps/web exec -- playwright install chromium",
            ),
        ],
    )
    def test_preserves_npm_package_directory_from_setup_install(
        self,
        setup_command: str,
        expected: str,
    ) -> None:
        command = playwright_browser_install_command(
            _profile_with_setup_and_browsers([setup_command])
        )

        assert command is not None
        assert command.command == expected
        assert command.required is False
        assert (
            browser_probe_workdir(_profile_with_setup_and_browsers([setup_command]))
            == "/workspace/apps/web"
        )

    @pytest.mark.parametrize(
        ("setup_command", "expected"),
        [
            (
                "npm --workspace apps/web ci",
                "npm --workspace apps/web exec -- playwright install chromium",
            ),
            (
                "npm -w apps/web ci",
                "npm -w apps/web exec -- playwright install chromium",
            ),
        ],
    )
    def test_preserves_npm_workspace_selector_without_using_as_probe_directory(
        self,
        setup_command: str,
        expected: str,
    ) -> None:
        command = playwright_browser_install_command(
            _profile_with_setup_and_browsers([setup_command])
        )

        assert command is not None
        assert command.command == expected
        assert command.required is False
        assert (
            browser_probe_workdir(_profile_with_setup_and_browsers([setup_command])) == "/workspace"
        )

    @pytest.mark.parametrize(
        "setup_command",
        [
            "yarn workspace web install",
            "yarn workspaces focus web",
        ],
    )
    def test_preserves_yarn_workspace_scope_without_using_as_probe_directory(
        self,
        setup_command: str,
    ) -> None:
        profile = _profile_with_setup_and_browsers([setup_command])

        command = playwright_browser_install_command(profile)

        assert command is not None
        assert command.command == "yarn workspace web playwright install chromium"
        assert command.required is False
        assert browser_probe_workdir(profile) == "/workspace"

    def test_unscoped_npm_install_keeps_npx_playwright_command(self) -> None:
        command = playwright_browser_install_command(_profile_with_setup_and_browsers(["npm ci"]))

        assert command is not None
        assert command.command == "npx playwright install chromium"
        assert command.timeout_seconds == 900

    def test_python_playwright_profile_uses_python_browser_install_without_node_package_manager(
        self,
    ) -> None:
        profile = _profile_with_setup_validate_and_browsers(
            setup=["python -m pip install playwright"],
            validate=["pytest --browser chromium"],
        )

        command = playwright_browser_install_command(profile)

        assert command is not None
        assert command.command == "python -m playwright install chromium"
        assert command.required is False

    def test_python_playwright_profile_preserves_detected_python_executable(self) -> None:
        profile = _profile_with_setup_validate_and_browsers(
            setup=["python3 -m pip install playwright"],
            validate=["pytest --browser chromium"],
        )

        command = playwright_browser_install_command(profile)

        assert command is not None
        assert command.command == "python3 -m playwright install chromium"
        assert command.required is False

    @pytest.mark.parametrize(
        ("setup_command", "expected_command"),
        [
            (
                ".venv/bin/python -m pip install playwright",
                ".venv/bin/python -m playwright install chromium",
            ),
            (
                ".venv/bin/pip install playwright",
                ".venv/bin/python -m playwright install chromium",
            ),
            (
                ".venv/bin/pip3.12 install playwright",
                ".venv/bin/python3.12 -m playwright install chromium",
            ),
        ],
    )
    def test_python_playwright_profile_preserves_path_qualified_install_executable(
        self,
        setup_command: str,
        expected_command: str,
    ) -> None:
        profile = _profile_with_setup_validate_and_browsers(
            setup=[setup_command],
            validate=[".venv/bin/pytest --browser chromium"],
        )

        command = playwright_browser_install_command(profile)

        assert command is not None
        assert command.command == expected_command
        assert command.required is False

    def test_python_playwright_profile_preserves_path_qualified_pytest_executable(
        self,
    ) -> None:
        profile = _profile_with_setup_validate_and_browsers(
            setup=[],
            validate=[
                ".venv/bin/pytest --browser chromium",
                "python -m pip install playwright",
            ],
        )

        command = playwright_browser_install_command(profile)

        assert command is not None
        assert command.command == ".venv/bin/python -m playwright install chromium"
        assert command.required is False

    @pytest.mark.parametrize(
        ("setup_command", "expected_command"),
        [
            ("python3.12 -m pip install playwright", "python3.12 -m playwright install chromium"),
            ("pip3.12 install playwright", "python3.12 -m playwright install chromium"),
        ],
    )
    def test_python_playwright_profile_preserves_versioned_python_or_pip_executable(
        self,
        setup_command: str,
        expected_command: str,
    ) -> None:
        profile = _profile_with_setup_validate_and_browsers(
            setup=[setup_command],
            validate=["pytest --browser chromium"],
        )

        command = playwright_browser_install_command(profile)

        assert command is not None
        assert command.command == expected_command
        assert command.required is False

    def test_python_playwright_profile_preserves_uv_project_environment(self) -> None:
        profile = _profile_with_setup_validate_and_browsers(
            setup=["uv add playwright"],
            validate=["uv run pytest --browser chromium"],
        )

        command = playwright_browser_install_command(profile)

        assert command is not None
        assert command.command == "uv run -m playwright install chromium"
        assert command.required is False

    @pytest.mark.parametrize(
        ("setup_command", "expected_command"),
        [
            (
                "uv pip install --system playwright",
                "python -m playwright install chromium",
            ),
            (
                "uv pip install --python python3.12 playwright",
                "python3.12 -m playwright install chromium",
            ),
        ],
    )
    def test_python_playwright_profile_preserves_uv_pip_target_environment(
        self,
        setup_command: str,
        expected_command: str,
    ) -> None:
        profile = _profile_with_setup_validate_and_browsers(
            setup=[setup_command],
            validate=["python -m pytest --browser chromium"],
        )

        command = playwright_browser_install_command(profile)

        assert command is not None
        assert command.command == expected_command
        assert command.required is False

    def test_python_playwright_profile_preserves_scoped_uv_project_environment(
        self,
    ) -> None:
        profile = _profile_with_setup_validate_and_browsers(
            setup=["cd apps/web && uv add playwright"],
            validate=["cd apps/web && uv run pytest --browser chromium"],
        )

        command = playwright_browser_install_command(profile)

        assert command is not None
        assert command.command == "cd apps/web && uv run -m playwright install chromium"
        assert command.required is False

    @pytest.mark.parametrize(
        ("setup_command", "requirements_content"),
        [
            ("python -m pip install -r requirements.txt", "pytest-playwright==0.5.0\n"),
            ("pip install --requirement requirements.txt", "playwright>=1.42\n"),
            (
                "python -m pip install -r requirements.txt",
                'playwright==1.42; python_version >= "3.8"\n',
            ),
            (
                "pip install --requirement requirements.txt",
                'pytest-playwright; sys_platform == "linux"\n',
            ),
            ("pip install -rrequirements.txt", "pytest-playwright\n"),
            ("pip install --requirement=requirements.txt", "playwright\n"),
        ],
    )
    def test_python_playwright_profile_detects_pip_requirement_files(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
        setup_command: str,
        requirements_content: str,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "requirements.txt").write_text(requirements_content, encoding="utf-8")
        profile = _profile_with_setup_validate_and_browsers(
            setup=[setup_command],
            validate=["pytest --browser chromium"],
        )

        command = playwright_browser_install_command(profile)

        assert command is not None
        assert command.command == "python -m playwright install chromium"
        assert command.required is False

    def test_python_playwright_profile_detects_nested_pip_requirement_file(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "requirements.txt").write_text("-r nested.txt\n", encoding="utf-8")
        (tmp_path / "nested.txt").write_text("playwright # browser runtime\n", encoding="utf-8")
        profile = _profile_with_setup_validate_and_browsers(
            setup=["pip install -r requirements.txt"],
            validate=["pytest --browser chromium"],
        )

        command = playwright_browser_install_command(profile)

        assert command is not None
        assert command.command == "python -m playwright install chromium"

    def test_python_playwright_profile_resolves_requirements_from_workspace_root(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        project_root = tmp_path / "project"
        host_root = tmp_path / "host"
        project_root.mkdir()
        host_root.mkdir()
        monkeypatch.chdir(host_root)
        (project_root / "requirements.txt").write_text("playwright\n", encoding="utf-8")
        profile = _profile_with_setup_validate_and_browsers(
            setup=["pip install -r requirements.txt"],
            validate=["pytest --browser chromium"],
        )

        commands = profile_phase_command_plan(
            profile,
            ["validate"],
            workspace_root=project_root,
        )

        assert [(command.phase, command.command.command) for command in commands] == [
            ("setup", "python -m playwright install chromium"),
            ("validate", "pytest --browser chromium"),
        ]

    def test_python_playwright_profile_maps_container_workspace_requirement_path(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        project_root = tmp_path / "project"
        host_root = tmp_path / "host"
        project_root.mkdir()
        host_root.mkdir()
        monkeypatch.chdir(host_root)
        (project_root / "requirements.txt").write_text("playwright\n", encoding="utf-8")
        profile = _profile_with_setup_validate_and_browsers(
            setup=["python -m pip install -r /workspace/requirements.txt"],
            validate=["pytest --browser chromium"],
        )

        command = playwright_browser_install_command(profile, workspace_root=project_root)

        assert command is not None
        assert command.command == "python -m playwright install chromium"
        assert command.required is False

    def test_browser_probe_deferral_resolves_validate_requirements_from_workspace_root(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        project_root = tmp_path / "project"
        host_root = tmp_path / "host"
        project_root.mkdir()
        host_root.mkdir()
        monkeypatch.chdir(host_root)
        (project_root / "requirements.txt").write_text("playwright\n", encoding="utf-8")
        profile = _profile_with_setup_validate_and_browsers(
            setup=["node scripts/generate-config.js"],
            validate=["pip install -r requirements.txt", "pytest --browser chromium"],
        )

        assert runtime_browser_probe_deferred_until_validate(
            profile,
            workspace_root=project_root,
        )

    @pytest.mark.parametrize(
        "setup_command",
        [
            "pip install -r playwright",
            "pip install --requirement ../requirements.txt",
        ],
    )
    def test_python_playwright_profile_ignores_unreadable_pip_requirement_files(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
        setup_command: str,
    ) -> None:
        project_root = tmp_path / "project"
        project_root.mkdir()
        monkeypatch.chdir(project_root)
        (tmp_path / "requirements.txt").write_text("playwright\n", encoding="utf-8")
        profile = _profile_with_setup_validate_and_browsers(
            setup=[setup_command],
            validate=["pytest --browser chromium"],
        )

        command = playwright_browser_install_command(profile)

        assert command is not None
        assert command.command == "npx playwright install chromium"

    def test_python_playwright_profile_rejects_host_absolute_requirement_outside_workspace(
        self,
        tmp_path: Path,
    ) -> None:
        project_root = tmp_path / "project"
        outside_root = tmp_path / "outside"
        project_root.mkdir()
        outside_root.mkdir()
        outside_requirements = outside_root / "requirements.txt"
        outside_requirements.write_text("playwright\n", encoding="utf-8")
        profile = _profile_with_setup_validate_and_browsers(
            setup=[f"python -m pip install -r {outside_requirements}"],
            validate=["pytest --browser chromium"],
        )

        command = playwright_browser_install_command(profile, workspace_root=project_root)

        assert command is not None
        assert command.command == "npx playwright install chromium"

    def test_python_playwright_profile_ignores_unrelated_node_install(
        self,
    ) -> None:
        profile = _profile_with_setup_validate_and_browsers(
            setup=["npm install", "python -m pip install playwright"],
            validate=["pytest --browser chromium"],
        )

        command = playwright_browser_install_command(profile)

        assert command is not None
        assert command.command == "python -m playwright install chromium"

        commands = profile_phase_command_plan(profile, ["setup", "validate"])

        assert [(command.phase, command.command.command) for command in commands] == [
            ("setup", "npm install"),
            ("setup", "python -m pip install playwright"),
            ("setup", "python -m playwright install chromium"),
            ("validate", "pytest --browser chromium"),
        ]

    def test_node_playwright_consumer_takes_precedence_over_python_playwright_install(
        self,
    ) -> None:
        profile = _profile_with_setup_validate_and_browsers(
            setup=["python -m pip install playwright", "pnpm install --frozen-lockfile"],
            validate=["pnpm exec playwright test"],
        )

        command = playwright_browser_install_command(profile)

        assert command is not None
        assert command.command == "pnpm exec playwright install chromium"

    def test_preserves_package_directory_from_leading_cd_setup_install(self) -> None:
        profile = _profile_with_setup_and_browsers(["cd apps/web && npm ci"])

        command = playwright_browser_install_command(profile)

        assert command is not None
        assert command.command == "npm --prefix apps/web exec -- playwright install chromium"
        assert browser_probe_workdir(profile) == "/workspace/apps/web"

    def test_preserves_pnpm_package_directory_from_leading_cd_setup_install(self) -> None:
        profile = _profile_with_setup_and_browsers(["cd apps/web && pnpm install"])

        command = playwright_browser_install_command(profile)

        assert command is not None
        assert command.command == "pnpm -C apps/web exec playwright install chromium"
        assert browser_probe_workdir(profile) == "/workspace/apps/web"

    @pytest.mark.parametrize(
        "setup_command",
        [
            "cd apps/web; pnpm install",
            "set -e; cd apps/web; pnpm install",
        ],
    )
    def test_preserves_pnpm_package_directory_from_leading_cd_shell_list_install(
        self,
        setup_command: str,
    ) -> None:
        profile = _profile_with_setup_and_browsers([setup_command])

        command = playwright_browser_install_command(profile)

        assert command is not None
        assert command.command == "pnpm -C apps/web exec playwright install chromium"
        assert browser_probe_workdir(profile) == "/workspace/apps/web"

    def test_preserves_cd_scoped_pnpm_install_after_inline_env_assignment(self) -> None:
        profile = _profile_with_setup_and_browsers(["cd apps/web && CI=1 pnpm install"])

        command = playwright_browser_install_command(profile)

        assert command is not None
        assert command.command == "pnpm -C apps/web exec playwright install chromium"
        assert browser_probe_workdir(profile) == "/workspace/apps/web"

    def test_rejects_shell_expanded_cd_scope_for_pnpm_install(self) -> None:
        profile = _profile_with_setup_and_browsers(['cd "$APP_DIR" && pnpm install'])

        command = playwright_browser_install_command(profile)

        assert command is not None
        assert command.command == "pnpm exec playwright install chromium"
        assert browser_probe_workdir(profile) == "/workspace"

    def test_preserves_package_directory_from_leading_cd_with_double_dash(self) -> None:
        profile = _profile_with_setup_and_browsers(["cd -- apps/web && npm ci"])

        command = playwright_browser_install_command(profile)

        assert command is not None
        assert command.command == "npm --prefix apps/web exec -- playwright install chromium"
        assert browser_probe_workdir(profile) == "/workspace/apps/web"

    def test_preserves_compact_pnpm_package_directory_flag(self) -> None:
        profile = _profile_with_setup_and_browsers(["pnpm -Capps/web install"])

        command = playwright_browser_install_command(profile)

        assert command is not None
        assert command.command == "pnpm -Capps/web exec playwright install chromium"
        assert browser_probe_workdir(profile) == "/workspace/apps/web"

    def test_preserves_pnpm_long_directory_flag(self) -> None:
        profile = _profile_with_setup_and_browsers(["pnpm --dir apps/web install"])

        command = playwright_browser_install_command(profile)

        assert command is not None
        assert command.command == "pnpm --dir apps/web exec playwright install chromium"
        assert browser_probe_workdir(profile) == "/workspace/apps/web"

    def test_preserves_absolute_package_directory_from_setup_install(self) -> None:
        profile = _profile_with_setup_and_browsers(["npm --prefix /workspace/apps/web ci"])

        command = playwright_browser_install_command(profile)

        assert command is not None
        assert (
            command.command
            == "npm --prefix /workspace/apps/web exec -- playwright install chromium"
        )
        assert browser_probe_workdir(profile) == "/workspace/apps/web"

    @pytest.mark.parametrize(
        ("setup_command", "expected", "expected_workdir"),
        [
            (
                "corepack enable && yarn install --immutable",
                "yarn playwright install chromium",
                "/workspace",
            ),
            (
                "corepack enable && yarn --cwd apps/web install --immutable",
                "yarn --cwd apps/web playwright install chromium",
                "/workspace/apps/web",
            ),
            (
                "corepack enable && cd apps/web && yarn install --immutable",
                "yarn --cwd apps/web playwright install chromium",
                "/workspace/apps/web",
            ),
            (
                "cd apps/web && corepack enable && pnpm install --frozen-lockfile",
                "pnpm -C apps/web exec playwright install chromium",
                "/workspace/apps/web",
            ),
            (
                "corepack enable && pnpm --dir apps/web install --frozen-lockfile",
                "pnpm --dir apps/web exec playwright install chromium",
                "/workspace/apps/web",
            ),
            (
                "corepack enable && CI=1 pnpm install --frozen-lockfile",
                "pnpm exec playwright install chromium",
                "/workspace",
            ),
            (
                "corepack enable && corepack prepare pnpm@9 --activate && "
                "pnpm install --frozen-lockfile",
                "pnpm exec playwright install chromium",
                "/workspace",
            ),
            (
                "corepack install -g pnpm@9 && pnpm install --frozen-lockfile",
                "pnpm exec playwright install chromium",
                "/workspace",
            ),
            (
                "corepack use yarn@4 && yarn install --immutable",
                "yarn playwright install chromium",
                "/workspace",
            ),
            (
                "corepack enable && bun install --frozen-lockfile",
                "bunx playwright install chromium",
                "/workspace",
            ),
            (
                "corepack enable && bun install --cwd apps/web --frozen-lockfile",
                "cd apps/web && bunx playwright install chromium",
                "/workspace/apps/web",
            ),
            (
                "corepack enable && cd apps/web && bun install --frozen-lockfile",
                "cd apps/web && bunx playwright install chromium",
                "/workspace/apps/web",
            ),
        ],
    )
    def test_browser_install_scans_past_corepack_preamble(
        self,
        setup_command: str,
        expected: str,
        expected_workdir: str,
    ) -> None:
        profile = _profile_with_setup_and_browsers([setup_command])

        command = playwright_browser_install_command(profile)

        assert command is not None
        assert command.command == expected
        assert browser_probe_workdir(profile) == expected_workdir

    def test_browser_install_scans_past_prep_preamble_before_dependency_install(
        self,
    ) -> None:
        profile = _profile_with_setup_and_browsers(["node scripts/write-npmrc.js && pnpm install"])

        command = playwright_browser_install_command(profile)
        commands = profile_phase_command_plan(profile, ["setup"])

        assert command is not None
        assert command.command == "pnpm exec playwright install chromium"
        assert [(command.phase, command.command.command) for command in commands] == [
            ("setup", "node scripts/write-npmrc.js && pnpm install"),
            ("setup", "pnpm exec playwright install chromium"),
        ]

    @pytest.mark.parametrize(
        ("setup_command", "expected"),
        [
            ("'unterminated", "npx playwright install chromium"),
            ("FOO=bar", "npx playwright install chromium"),
            ("npm install && echo done", "npx playwright install chromium"),
            ("npm install @playwright/test", "npx playwright install chromium"),
            ("yarn", "yarn playwright install chromium"),
            ("cd", "npx playwright install chromium"),
            ("cd - && npm ci", "npx playwright install chromium"),
            ("cd apps/web npm ci", "npx playwright install chromium"),
            ("corepack disable && yarn install --immutable", "npx playwright install chromium"),
            ("corepack enable || yarn install --immutable", "npx playwright install chromium"),
            ("corepack enable", "npx playwright install chromium"),
            ("node scripts/write-npmrc.js || pnpm install", "npx playwright install chromium"),
            ("node scripts/write-npmrc.js & pnpm install", "npx playwright install chromium"),
        ],
    )
    def test_browser_install_defaults_or_accepts_edge_setup_forms(
        self,
        setup_command: str,
        expected: str,
    ) -> None:
        command = playwright_browser_install_command(
            _profile_with_setup_and_browsers([setup_command])
        )

        assert command is not None
        assert command.command == expected

    def test_malformed_package_manager_location_is_unknown(self) -> None:
        assert _node_package_manager_package_dir('npm "unterminated') is None

    def test_package_manager_location_skips_incomplete_location_flag(self) -> None:
        assert _node_package_manager_package_dir("npm --prefix") is None

    def test_package_manager_location_ignores_non_location_equals_option(self) -> None:
        assert _node_package_manager_package_dir("npm --cache=.npm-cache") is None

    def test_dependency_install_parser_skips_incomplete_location_flag(self) -> None:
        assert _node_dependency_install_package_manager("npm --prefix") is None

    def test_dependency_install_parser_ignores_unpreserved_equals_option(self) -> None:
        assert _node_dependency_install_package_manager("npm --userconfig=.npmrc ci") == "npm"

    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            ("npm add @playwright/test", "npm"),
            ("pnpm add @playwright/test", "pnpm"),
            ("yarn add @playwright/test", "yarn"),
            ("bun add @playwright/test", "bun"),
        ],
    )
    def test_dependency_install_parser_treats_add_as_install(
        self,
        command: str,
        expected: str,
    ) -> None:
        assert _node_dependency_install_package_manager(command) == expected

    @pytest.mark.parametrize(
        "command",
        [
            "node scripts/write-npmrc.js || pnpm install",
            "node scripts/write-npmrc.js | pnpm install",
            "node scripts/write-npmrc.js & pnpm install",
        ],
    )
    def test_dependency_install_parser_does_not_scan_past_unsafe_preambles(
        self,
        command: str,
    ) -> None:
        assert _node_dependency_install_package_manager(command) is None

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
        ("setup_command", "project_dir", "expected_install"),
        [
            (
                "uv sync --extra e2e",
                "",
                "uv run -m playwright install chromium",
            ),
            (
                "cd apps/web && uv sync --extra e2e",
                "apps/web",
                "cd apps/web && uv run -m playwright install chromium",
            ),
        ],
    )
    def test_uv_sync_extra_pyproject_dependency_uses_python_playwright(
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
        "setup_command",
        [
            "uv sync --group e2e",
            "uv sync --group=e2e",
            "uv sync --all-groups",
        ],
    )
    def test_uv_sync_dependency_group_uses_python_playwright(
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
        assert command.command == "uv run -m playwright install chromium"

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

    def test_post_agent_playwright_dependency_install_not_inserted_before_pre_agent(
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
            ("pre_agent", "pnpm exec playwright test"),
        ]

    def test_post_agent_python_playwright_dependency_install_defers_browser_install(
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


@pytest.mark.unit
class TestLeadingExecutable:
    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            ("ruff check .", "ruff"),
            ("python -m ruff check .", "python"),
            ("FOO=bar ruff check .", "ruff"),
            ("FOO=bar BAZ=qux mypy src", "mypy"),
            ("/usr/local/bin/pytest -q", "/usr/local/bin/pytest"),
            # A YAML block command opening with a shell comment line runs the
            # real command under ``sh -lc`` (which ignores ``#`` comments), so
            # the probe must look past the comment to the actual executable.
            ("# run lint\nruff check .", "ruff"),
            ("  # leading comment\nmypy src", "mypy"),
            ("ruff check . # trailing comment", "ruff"),
            # A required validate block guarded by a leading shell-option command
            # (``set -e``, ``set -euo pipefail``, ``shopt -s globstar``) runs the
            # real tool after the guard under ``sh -lc``; the probe must look past
            # the guard statement to the tool that follows, otherwise a missing
            # toolchain slips past the handoff and only fails later in
            # ``monitoring_pr`` instead of as PROFILE_VALIDATE_TOOLCHAIN_UNPROVISIONED.
            ("set -e; ruff check .", "ruff"),
            ("set -euo pipefail && ruff check .", "ruff"),
            ("set -euo pipefail\nruff check .", "ruff"),
            ("shopt -s globstar; ruff check .", "ruff"),
            ("umask 022; mypy src", "mypy"),
            # Multiple stacked guards are all skipped to reach the real tool.
            ("set -e; set -x; ruff check .", "ruff"),
            # A guard followed by an env-assignment prefix still probes the tool.
            ("set -e; FOO=bar ruff check .", "ruff"),
            # The common ``env`` wrapper forms name the real tool *after* env's own
            # assignments. Probing the wrapper (``env``) only checks ``command -v
            # env`` — almost always present — so an uninstalled pytest/ruff would
            # slip past the handoff and die later in ``monitoring_pr``. Unwrap env
            # and probe the program it execs.
            ("env PYTHONPATH=src pytest -q", "pytest"),
            ("/usr/bin/env pytest -q", "pytest"),
            ("env pytest", "pytest"),
            ("env FOO=bar PYTHONPATH=src mypy src", "mypy"),
            # A leading shell guard before the env wrapper is still skipped.
            ("set -e; env PYTHONPATH=src pytest -q", "pytest"),
            # A top-level pipeline exits with its *final* stage's status under
            # ``sh -lc`` (no ``pipefail``), so the exit-determining final stage is
            # the probe target — not the masked leading tool. An unprovisioned
            # final-stage reporter (``pytest -q | custom-reporter``) really exits
            # ``127`` and must be caught. Both the spaced and the ``shlex``-glued
            # pipe forms are recognised, and a multi-stage pipeline probes its last.
            ("pytest -q | tee pytest.log", "tee"),
            ("pytest -q | custom-reporter", "custom-reporter"),
            ("cat data.json | jq .", "jq"),
            ("pytest -q|tee pytest.log", "tee"),
            ("a | b | c", "c"),
        ],
    )
    def test_extracts_leading_executable(self, command: str, expected: str) -> None:
        assert _leading_executable(command) == expected

    @pytest.mark.parametrize(
        "command",
        [
            "cd build",
            ": no-op",
            "echo done",
            "export PATH=/x",
            "FOO=bar",  # only assignments, no executable
            'ruff "check',  # unbalanced quote -> shlex parse failure
            # Shell keywords / builtins leading a compound command: the command
            # cannot be reduced to a single probeable tool, so it fails open
            # rather than treating the keyword (``for``, ``if``) as a fake tool.
            "if [ -f x ]; then ruff; fi",
            "for f in *.py; do ruff $f; done",
            "while read -r line; do echo $line; done",
            "until make; do sleep 1; done",
            "case $x in a) ruff;; esac",
            "[[ -f pyproject.toml ]] && ruff check .",
            "select f in *.py; do ruff $f; done",
            "time ruff check .",
            "exit 0",
            "pwd",
            "printf '%s\\n' done",
            "read -r answer",
            "function lint { ruff; }",
            "command ruff check .",
            # A leading ``PATH=...`` env assignment is what makes the executable
            # resolvable, but the shared-PATH ``command -v`` probe cannot replay a
            # per-command PATH prefix, so the command fails open rather than being
            # falsely reported PROFILE_VALIDATE_TOOLCHAIN_UNPROVISIONED.
            "PATH=/workspace/node_modules/.bin:$PATH eslint .",
            "FOO=bar PATH=/opt/bin:$PATH mypy src",
            # A leading subshell opener is shell grouping the runner executes
            # under ``sh -lc``; ``shlex`` glues the ``(`` to the first word
            # (``(cd``) or keeps it standalone (``(``), so probing it would
            # falsely report PROFILE_VALIDATE_TOOLCHAIN_UNPROVISIONED. Fail open.
            "(cd frontend && npm test)",
            "( cd frontend && npm test )",
            # A leading token that names the executable via shell expansion —
            # tilde or parameter/command substitution — is expanded by the
            # ``sh -lc`` the real runner uses, but ``shlex`` keeps the literal
            # token and the probe passes it quoted to ``command -v "$t"`` where
            # it is not re-expanded. Probing it would falsely report
            # PROFILE_VALIDATE_TOOLCHAIN_UNPROVISIONED, so fail open.
            "~/bin/ruff check .",
            "$HOME/.local/bin/ruff check .",
            "${HOME}/bin/ruff check .",
            "FOO=bar $HOME/.local/bin/mypy src",
            "`which ruff` check .",
            # A command that is nothing but a shell comment reduces to no tokens
            # under ``sh -lc``; there is no executable to probe, so fail open.
            "# just a note, no command here",
            # A leading shell *guard* (``set -e``) is skipped, but only guard
            # statements are — a leading ``cd`` changes the working directory the
            # tool resolves against, so a ``cd``-prefixed sequence keeps the
            # existing fail-open behavior rather than probing the later tool under
            # the wrong directory.
            "cd build; ruff check .",
            # A guard with no following command names no tool, so fail open.
            "set -euo pipefail",
            "shopt -s globstar",
            # ``env`` wrapper forms the shared-PATH probe cannot faithfully
            # replay fail open after unwrapping rather than probing ``env``:
            # ``-i`` clears the environment (and thus PATH), any other option
            # flag (``-u NAME``) consumes a value the parser cannot place, an
            # ``env PATH=...`` binding makes the tool resolvable off the shared
            # PATH, and an ``env``-wrapped shell-expanded path is expanded only by
            # the real ``sh -lc``.
            "env -i pytest",
            "env -u PATH pytest",
            "env PATH=/opt/bin:$PATH eslint .",
            "env $HOME/.local/bin/ruff check .",
            # An ``env`` wrapper that names only assignments (or nothing) execs no
            # program, so there is no tool to probe.
            "env",
            "env FOO=bar",
            # A top-level pipeline exits with its *final* stage's status under
            # ``sh -lc`` (no ``pipefail``), so the final stage is probed (see
            # ``test_extracts_leading_executable``) while the masked leading stages
            # fail open. A pipeline whose *final* stage is itself un-probeable — a
            # shell-expanded path or a builtin — has no probeable exit-determining
            # tool, so the whole pipeline fails open.
            "pytest -q | $HOME/bin/reporter",
            "cat data.json | cd build",
        ],
    )
    def test_unprobeable_leading_token_returns_none(self, command: str) -> None:
        assert _leading_executable(command) is None

    def test_non_path_assignments_still_probe_the_executable(self) -> None:
        # Only a PATH-binding assignment forces fail-open; other env assignments
        # leave the executable probeable under the shared PATH.
        assert _leading_executable("PYTHONPATH=/x FOO=bar ruff check .") == "ruff"


@pytest.mark.unit
class TestLeadingExecutables:
    def test_single_command_yields_one_tool(self) -> None:
        assert _leading_executables("ruff check .") == ["ruff"]

    def test_compound_command_yields_every_chained_tool(self) -> None:
        # A single validate command that chains tools with ``&&`` must be probed
        # for *all* of them — otherwise a later chained tool that is off PATH
        # slips past the handoff and fails later in ``monitoring_pr`` instead of
        # early with PROFILE_VALIDATE_TOOLCHAIN_UNPROVISIONED.
        assert _leading_executables("ruff check . && mypy src") == ["ruff", "mypy"]

    def test_semicolon_list_probes_only_the_final_segment(self) -> None:
        # A ``;`` list exits with its *final* member's status under ``sh -lc``
        # (``a; b`` -> b's status), so a non-final segment's failure (a missing
        # tool's ``127`` included) is masked when the final segment succeeds.
        # ``ruff check .; pytest -q`` therefore probes only ``pytest`` — probing
        # the masked ``ruff`` would falsely report
        # PROFILE_VALIDATE_TOOLCHAIN_UNPROVISIONED for a profile that passes.
        assert _leading_executables("ruff check .; pytest -q") == ["pytest"]

    def test_semicolon_list_final_segment_or_list_fails_open(self) -> None:
        # The final ``;``-segment is exit-determining, but an OR-list
        # (``black --check . || mypy src``) still fails open because a missing
        # tool's failure is masked by a succeeding member; the masked non-final
        # ``ruff`` segment fails open too, so nothing is probed.
        assert _leading_executables("ruff check .; black --check . || mypy src") == []

    def test_set_e_makes_every_semicolon_segment_required(self) -> None:
        # With ``set -e`` active, a ``;`` list aborts on the first failure, so
        # every following segment is exit-determining and each leading tool is
        # required. ``set -e; ruff check .; pytest -q`` probes both ``ruff`` and
        # ``pytest``; the guard itself names no tool.
        assert _leading_executables("set -e; ruff check .; pytest -q") == ["ruff", "pytest"]

    def test_set_o_errexit_makes_every_semicolon_segment_required(self) -> None:
        # The long ``set -o errexit`` form enables errexit just like ``set -e``.
        assert _leading_executables("set -o errexit\nruff check .\npytest -q") == [
            "ruff",
            "pytest",
        ]

    def test_set_x_alone_does_not_make_segments_required(self) -> None:
        # ``set -x`` (xtrace) is a guard that enables no errexit, so the non-final
        # ``ruff`` stays masked and only the final ``pytest`` is probed.
        assert _leading_executables("set -x; ruff check .; pytest -q") == ["pytest"]

    def test_set_o_pipefail_alone_does_not_make_segments_required(self) -> None:
        # ``set -o pipefail`` (without ``errexit``) does not abort the list on a
        # failed command, so the non-final ``ruff`` stays masked and only the
        # final ``pytest`` is probed.
        assert _leading_executables("set -o pipefail; ruff check .; pytest -q") == ["pytest"]

    def test_trailing_separator_keeps_the_real_segment_exit_determining(self) -> None:
        # A trailing ``;``/newline (a YAML block scalar's trailing newline) leaves
        # an empty segment that executes nothing, so the real command before it is
        # still the exit-determining final segment and is probed.
        assert _leading_executables("ruff check .\n") == ["ruff"]
        assert _leading_executables("ruff check .;") == ["ruff"]

    def test_or_list_fails_open(self) -> None:
        # ``ruff check . || true`` exits 0 under ``sh -lc`` even when ruff is
        # absent, so probing ruff would falsely report
        # PROFILE_VALIDATE_TOOLCHAIN_UNPROVISIONED for a passing profile.
        assert _leading_executables("ruff check . || true") == []

    def test_or_list_skips_every_member(self) -> None:
        assert _leading_executables("black --check . || mypy src") == []

    def test_and_segment_ending_in_or_fails_open(self) -> None:
        # ``(ruff && mypy) || true``: either tool's failure is masked by the
        # trailing ``|| true``, so the whole segment fails open.
        assert _leading_executables("ruff check . && mypy src || true") == []

    def test_or_arm_before_and_tail_probes_the_and_tail(self) -> None:
        # ``&&``/``||`` share precedence and associate left-to-right, so
        # ``ruff check . || true && mypy src`` parses as
        # ``((ruff || true) && mypy)``: ``mypy`` *always* runs and its ``127``
        # fails the command, so it is required even though the segment contains a
        # top-level ``||``. Only ``ruff`` (masked by ``|| true``) fails open.
        assert _leading_executables("ruff check . || true && mypy src") == ["mypy"]

    def test_or_then_and_chain_keeps_the_trailing_and_run(self) -> None:
        # ``black --check . || mypy src && pytest`` -> ``((black || mypy) && pytest)``:
        # only ``pytest`` is guaranteed to run; ``black``/``mypy`` are masked by the
        # left-hand ``||`` group.
        assert _leading_executables("black --check . || mypy src && pytest") == ["pytest"]

    def test_only_the_final_semicolon_segment_is_probed(self) -> None:
        # ``ruff check .; black --check . || true; flake8 .``: only the final
        # ``flake8`` segment sets the command's exit status under ``sh -lc``
        # (no ``set -e``). The masked ``ruff`` and the ``|| true``-masked ``black``
        # both fail open, so only ``flake8`` is probed.
        assert _leading_executables("ruff check .; black --check . || true; flake8 .") == [
            "flake8",
        ]

    def test_leading_guard_is_skipped_before_chained_tools(self) -> None:
        assert _leading_executables("set -euo pipefail && ruff check . && mypy src") == [
            "ruff",
            "mypy",
        ]

    def test_unprobeable_statement_stops_collection_keeping_earlier_tools(self) -> None:
        # A directory-changing ``cd`` ends collection because tools after it
        # resolve against a different directory, but the tool collected before it
        # is still probed rather than discarded.
        assert _leading_executables("ruff check . && cd build && mypy") == ["ruff"]

    def test_leading_unprobeable_token_yields_no_tools(self) -> None:
        assert _leading_executables("cd build && ruff check .") == []

    def test_comment_only_command_yields_no_tools(self) -> None:
        assert _leading_executables("# just a note, no command here") == []

    def test_operator_inside_trailing_comment_is_not_a_split_point(self) -> None:
        # ``sh -lc`` ignores the whole ``# ...`` comment, so an operator inside it
        # is not a real terminator. Splitting on the comment's ``&&`` would probe
        # the fragment after it (``tests``) as a required executable and falsely
        # report PROFILE_VALIDATE_TOOLCHAIN_UNPROVISIONED for a profile that only
        # runs ``ruff``.
        assert _leading_executables("ruff check .  # run lint && tests") == ["ruff"]

    def test_operator_inside_leading_comment_line_is_not_a_split_point(self) -> None:
        # The comment line runs nothing under ``sh -lc``; only the command on the
        # next line is probed, never the ``tests`` fragment after the comment's
        # ``&&``.
        assert _leading_executables("# run lint && tests\nruff check .") == ["ruff"]

    def test_hash_inside_a_word_is_literal_not_a_comment(self) -> None:
        # ``#`` only opens a comment at a word boundary, matching ``sh -lc``: in
        # ``echo a#b`` it stays part of the word, so the ``&&`` after it is a real
        # split point and the (builtin) leading ``echo`` still fails open.
        assert _leading_executables("echo a#b && ruff check .") == []

    def test_top_level_pipeline_probes_only_the_final_stage(self) -> None:
        # ``pytest -q | tee pytest.log`` exits with ``tee``'s status under
        # ``sh -lc`` (no ``pipefail``), so ``tee`` (the exit-determining final
        # stage) is the real probe target while the masked leading ``pytest`` fails
        # open. A missing final-stage tool — an unprovisioned reporter — really
        # exits ``127`` and must be caught at handoff as
        # PROFILE_VALIDATE_TOOLCHAIN_UNPROVISIONED rather than dying later in
        # ``monitoring_pr``.
        assert _leading_executables("pytest -q | tee pytest.log") == ["tee"]
        assert _leading_executables("pytest -q | custom-reporter") == ["custom-reporter"]

    def test_pipeline_with_unprobeable_final_stage_fails_open(self) -> None:
        # When the *final* stage is itself un-probeable (a shell-expanded path the
        # shared-PATH probe cannot replay, or a builtin), the pipeline has no
        # probeable exit-determining tool, so it fails open rather than guessing.
        assert _leading_executables("pytest -q | $HOME/bin/reporter") == []
        assert _leading_executables("cat data.json | cd build") == []

    def test_pipeline_split_respects_quotes_and_escapes(self) -> None:
        # The pipe-splitter mirrors the quote/escape handling of the statement
        # splitter: a ``|`` inside single/double quotes or escaped is part of a
        # stage, not a stage boundary, so only the real top-level pipe splits and
        # the exit-determining final stage's tool (``wc``) is probed.
        assert _leading_executables('grep "a|b" file | wc -l') == ["wc"]
        assert _leading_executables("grep 'a|b' file | wc -l") == ["wc"]
        assert _leading_executables("grep a\\|b file | wc -l") == ["wc"]
        assert _leading_executables('echo "a\\"b" | wc -l') == ["wc"]

    def test_pipeline_skips_only_itself_in_a_semicolon_list(self) -> None:
        # A non-final ``;``-segment is masked, so even its pipeline's
        # exit-determining final stage (``tee``) is dropped; the pipe does not
        # affect how the later ``;``-separated command resolves, so the final
        # segment's ``ruff`` is still probed.
        assert _leading_executables("pytest -q | tee log; ruff check .") == ["ruff"]

    def test_pipeline_in_and_chain_probes_final_stage_then_chained_tool(self) -> None:
        # ``pytest -q | tee log && ruff check .``: the pipeline's exit is ``tee``'s
        # status (its exit-determining final stage), and that gates ``ruff`` via
        # ``&&``, whose ``127`` would also fail the command. So both ``tee`` and
        # ``ruff`` are required, while the masked piped ``pytest`` fails open.
        assert _leading_executables("pytest -q | tee log && ruff check .") == ["tee", "ruff"]

    def test_pipe_after_a_required_tool_probes_the_pipeline_final_stage(self) -> None:
        # ``ruff check . && pytest -q | tee log``: ``ruff`` runs first and its
        # absence fails the ``&&`` chain, so it is probed; the trailing pipeline's
        # exit-determining final stage ``tee`` is probed too, while its masked
        # leading ``pytest`` fails open.
        assert _leading_executables("ruff check . && pytest -q | tee log") == ["ruff", "tee"]

    def test_env_wrapped_chained_tools_probe_each_real_program(self) -> None:
        # Each ``&&``-chained ``env`` wrapper is unwrapped to the program it
        # execs, so both real tools are probed rather than ``env`` twice.
        assert _leading_executables(
            "env PYTHONPATH=src pytest -q && /usr/bin/env ruff check ."
        ) == ["pytest", "ruff"]

    def test_heredoc_body_stays_attached_probing_the_opener_tool(self) -> None:
        # ``python - <<'PY' ... PY`` is fed whole to ``sh -lc``: the body and the
        # closing ``PY`` delimiter are stdin for ``python``, not new statements.
        # Keeping the heredoc body attached to its opener keeps ``python`` the
        # probe target rather than the delimiter word ``PY`` — which would
        # otherwise falsely report PROFILE_VALIDATE_TOOLCHAIN_UNPROVISIONED.
        assert _leading_executables("python - <<'PY'\nimport os\nprint(os.getcwd())\nPY") == [
            "python"
        ]

    def test_quoted_and_unquoted_heredoc_delimiters_attach_body(self) -> None:
        # Both bare (``<<SQL``) and ``<<-`` tab-stripping delimiters are consumed
        # with their body so the opener tool is probed, never a body line.
        assert _leading_executables("psql -d awf <<SQL\nSELECT 1;\nSQL") == ["psql"]
        assert _leading_executables("cat <<-EOF\n\tline\n\tEOF") == ["cat"]

    def test_command_after_heredoc_delimiter_is_split_normally(self) -> None:
        # Once the heredoc closes, the following ``&&`` tool resumes normal
        # splitting and is probed alongside the opener.
        assert _leading_executables("python - <<'PY'\nprint(1)\nPY\n && ruff check .") == [
            "python",
            "ruff",
        ]

    def test_here_string_is_not_treated_as_a_heredoc(self) -> None:
        # ``<<<`` is a here-string with no body; the next newline still splits and
        # the final segment's tool is probed as usual.
        assert _leading_executables("grep x <<< value\nruff check .") == ["ruff"]

    def test_heredoc_with_spaced_delimiter_attaches_body(self) -> None:
        # POSIX allows blanks between ``<<`` and the delimiter word
        # (``cat << EOF``); the splitter skips that whitespace, reads ``EOF`` as the
        # delimiter, and keeps the body and closing delimiter line attached so the
        # opener ``cat`` stays the probe target rather than a body line or the
        # delimiter word ``EOF``.
        assert _leading_executables("cat << EOF\nbody line\nEOF") == ["cat"]

    def test_heredoc_with_backslash_escaped_delimiter_attaches_body(self) -> None:
        # The ``cat <<\EOF`` form (a backslash before the delimiter) is the
        # "quote the delimiter" spelling that disables expansion in the body; the
        # backslash escape is consumed so the delimiter is still ``EOF`` and the
        # body attaches, keeping ``cat`` the probe target rather than the delimiter
        # word or a body line.
        assert _leading_executables("cat <<\\EOF\nbody line\nEOF") == ["cat"]

    def test_unterminated_quoted_heredoc_delimiter_fails_open(self) -> None:
        # A malformed opener whose quoted delimiter is never closed
        # (``cat <<'EOF`` with no closing quote) consumes the remaining input as
        # the delimiter word, so the statement names no clean leading tool; the
        # splitter fails open (no probe target) rather than reporting a body line
        # or the delimiter word as a missing toolchain.
        assert _leading_executables("cat <<'EOF\nbody line\nEOF") == []

    def test_empty_quoted_heredoc_delimiter_is_not_tracked(self) -> None:
        # An empty quoted delimiter (``cat <<''``) names no heredoc word, so no
        # heredoc body is tracked and the following newline splits the list
        # normally; under ``sh -lc`` the final ``;``-segment (``body line``) is the
        # exit-determining one, so it — not the opener ``cat`` — is the probe
        # target. The degenerate empty delimiter is the documented fallback.
        assert _leading_executables("cat <<''\nbody line") == ["body"]

    def test_unclosed_heredoc_body_runs_to_end_keeping_the_opener(self) -> None:
        # A heredoc whose closing delimiter never appears before end-of-input
        # (``cat <<EOF`` followed by a body with no terminating ``EOF`` line) still
        # has its whole body attached to the opener statement, so ``cat`` stays the
        # probe target rather than a body line being split off and probed.
        assert _leading_executables("cat <<EOF\nbody with no close") == ["cat"]

    def test_lone_brace_group_fails_open(self) -> None:
        # ``{ ruff check .; }`` is a POSIX brace group: ``sh -lc`` treats ``{``/``}``
        # as reserved words and the inner ``;`` as the group's own list separator.
        # The splitter keeps the group whole and fails open on the leading ``{``
        # rather than splitting off the closing ``}`` and probing
        # ``command -v '}'`` — a false PROFILE_VALIDATE_TOOLCHAIN_UNPROVISIONED.
        assert _leading_executables("{ ruff check .; }") == []
        assert _leading_executables("{ mypy src; pytest -q; }") == []

    def test_brace_group_after_and_chain_keeps_the_preceding_tool(self) -> None:
        # The tool required before the ``&&`` is still probed; only the brace group
        # itself fails open, so the closing ``}`` is never mistaken for a tool.
        assert _leading_executables("ruff check . && { mypy src; pytest -q; }") == ["ruff"]

    def test_set_e_brace_group_does_not_leak_inner_commands(self) -> None:
        # Under ``set -e`` every following segment is exit-determining, but the
        # brace group must still stay one statement so an inner command is never
        # split out and probed; the whole group fails open on its leading ``{``.
        assert _leading_executables("set -e; { false; pytest -q; }") == []

    def test_brace_expansion_is_not_a_brace_group(self) -> None:
        # ``cp a.{txt,bak}`` is brace expansion (no blank after ``{``), not a
        # group, so the real leading tool is still probed and chained tools follow.
        assert _leading_executables("cp a.{txt,bak} && ruff check .") == ["cp", "ruff"]

    def test_find_placeholder_brace_is_not_a_brace_group(self) -> None:
        # The ``find ... {} \;`` placeholder is a single ``{}`` argument, not a
        # group, so ``find`` is probed and the chained ``ruff`` follows.
        assert _leading_executables("find . -exec rm {} \\; && ruff check .") == [
            "find",
            "ruff",
        ]


@pytest.mark.unit
class TestValidateCommandProbeTargets:
    def test_empty_validate_phase_has_no_targets(self) -> None:
        assert validate_command_probe_targets(_profile_with_validate([])) == []

    def test_maps_each_validate_command_to_its_tool(self) -> None:
        targets = validate_command_probe_targets(
            _profile_with_validate(["ruff check .", "mypy src"])
        )
        assert [(t.tool, t.command) for t in targets] == [
            ("ruff", "ruff check ."),
            ("mypy", "mypy src"),
        ]

    def test_compound_command_probes_every_chained_tool(self) -> None:
        # A single required validate command chaining tools with ``&&`` yields a
        # probe target for each tool, all keeping the full command as the
        # representative for the operator message, so a later chained tool that
        # is off PATH fails the handoff early with
        # PROFILE_VALIDATE_TOOLCHAIN_UNPROVISIONED instead of slipping through.
        targets = validate_command_probe_targets(
            _profile_with_validate(["ruff check . && mypy src"])
        )
        assert [(t.tool, t.command) for t in targets] == [
            ("ruff", "ruff check . && mypy src"),
            ("mypy", "ruff check . && mypy src"),
        ]

    def test_dedupes_chained_tool_shared_across_commands(self) -> None:
        # A tool that appears both inside a compound command and as a standalone
        # command collapses to a single probe target, keeping the first command
        # that introduced it as the representative.
        targets = validate_command_probe_targets(
            _profile_with_validate(["ruff check . && mypy src", "mypy --strict src"])
        )
        assert [(t.tool, t.command) for t in targets] == [
            ("ruff", "ruff check . && mypy src"),
            ("mypy", "ruff check . && mypy src"),
        ]

    def test_dedupes_by_tool_keeping_first_command(self) -> None:
        # Two ruff commands collapse to a single probe target, keeping the first
        # command as the representative for the operator message.
        targets = validate_command_probe_targets(
            _profile_with_validate(["ruff check .", "ruff format --check ."])
        )
        assert [(t.tool, t.command) for t in targets] == [("ruff", "ruff check .")]

    def test_skips_unprobeable_commands(self) -> None:
        # A builtin/compound leading token is skipped (fail-open); only the
        # probeable command yields a target.
        targets = validate_command_probe_targets(
            _profile_with_validate(["cd build", "ruff check ."])
        )
        assert [(t.tool, t.command) for t in targets] == [("ruff", "ruff check .")]

    def test_skips_brace_group_commands(self) -> None:
        # A POSIX brace-group validate command (``{ ruff check .; }``) is shell
        # grouping syntax; it must never yield a ``}`` probe target, which would
        # fail the handoff with PROFILE_VALIDATE_TOOLCHAIN_UNPROVISIONED for a
        # profile whose grouped command passes. The plain command still probes.
        targets = validate_command_probe_targets(
            _profile_with_validate(["{ ruff check .; }", "mypy src"])
        )
        assert [(t.tool, t.command) for t in targets] == [("mypy", "mypy src")]

    def test_skips_path_modifying_commands(self) -> None:
        # A command whose env prefix binds PATH is what makes its executable
        # resolvable; the shared-PATH probe cannot replay that, so it is skipped
        # (fail-open) rather than failing the handoff with
        # PROFILE_VALIDATE_TOOLCHAIN_UNPROVISIONED. The plain command still probes.
        targets = validate_command_probe_targets(
            _profile_with_validate(
                ["PATH=/workspace/node_modules/.bin:$PATH eslint .", "ruff check ."]
            )
        )
        assert [(t.tool, t.command) for t in targets] == [("ruff", "ruff check .")]

    def test_skips_subshell_wrapped_commands(self) -> None:
        # A subshell-wrapped command (``(cd frontend && npm test)``) is shell
        # grouping the runner executes under ``sh -lc``; its leading token is
        # the glued ``(cd``, not a probeable tool, so it fails open rather than
        # failing the handoff with PROFILE_VALIDATE_TOOLCHAIN_UNPROVISIONED. The
        # plain command still probes.
        targets = validate_command_probe_targets(
            _profile_with_validate(["(cd frontend && npm test)", "ruff check ."])
        )
        assert [(t.tool, t.command) for t in targets] == [("ruff", "ruff check .")]

    def test_skips_shell_expanded_leading_token_commands(self) -> None:
        # A command whose leading executable is named via shell expansion
        # (``$HOME/.local/bin/ruff``) resolves under the runner's ``sh -lc`` but
        # not under the quoted ``command -v "$t"`` probe, so it fails open rather
        # than failing the handoff with PROFILE_VALIDATE_TOOLCHAIN_UNPROVISIONED.
        # The plain command still probes.
        targets = validate_command_probe_targets(
            _profile_with_validate(["$HOME/.local/bin/ruff check .", "mypy src"])
        )
        assert [(t.tool, t.command) for t in targets] == [("mypy", "mypy src")]

    def test_probes_real_tool_behind_env_wrapper(self) -> None:
        # A profile that runs its validate tool through the common ``env`` wrapper
        # (``env PYTHONPATH=src pytest -q``) must probe ``pytest``, not ``env`` —
        # otherwise an unprovisioned pytest passes the ``command -v env`` check and
        # dies later in ``monitoring_pr`` instead of failing the handoff with
        # PROFILE_VALIDATE_TOOLCHAIN_UNPROVISIONED. The full command stays as the
        # representative for the operator message.
        targets = validate_command_probe_targets(
            _profile_with_validate(["env PYTHONPATH=src pytest -q", "mypy src"])
        )
        assert [(t.tool, t.command) for t in targets] == [
            ("pytest", "env PYTHONPATH=src pytest -q"),
            ("mypy", "mypy src"),
        ]

    def test_probes_final_stage_of_top_level_pipeline_commands(self) -> None:
        # A validate command that pipes its tool into a logger
        # (``pytest -q | tee pytest.log``) exits with the *final* stage's status
        # under ``sh -lc`` (no ``pipefail``), so the exit-determining final stage
        # (``tee``) is probed while the masked leading ``pytest`` fails open. A
        # missing final-stage reporter really exits ``127`` and must fail the
        # handoff with PROFILE_VALIDATE_TOOLCHAIN_UNPROVISIONED rather than dying
        # later in ``monitoring_pr``.
        targets = validate_command_probe_targets(
            _profile_with_validate(["pytest -q | tee pytest.log", "ruff check ."])
        )
        assert [(t.tool, t.command) for t in targets] == [
            ("tee", "pytest -q | tee pytest.log"),
            ("ruff", "ruff check ."),
        ]

    def test_skips_pipeline_with_unprobeable_final_stage(self) -> None:
        # A pipeline whose *final* stage is itself un-probeable (a shell-expanded
        # path the shared-PATH probe cannot replay) has no probeable
        # exit-determining tool, so it fails open rather than failing the handoff
        # with PROFILE_VALIDATE_TOOLCHAIN_UNPROVISIONED; the plain command probes.
        targets = validate_command_probe_targets(
            _profile_with_validate(["pytest -q | $HOME/bin/reporter", "ruff check ."])
        )
        assert [(t.tool, t.command) for t in targets] == [("ruff", "ruff check .")]

    def test_probes_tool_after_leading_comment_line(self) -> None:
        # A YAML block command opening with a shell comment line (``# run lint``)
        # runs the real command under ``sh -lc``, which ignores the comment, so
        # the probe must target the actual tool rather than the literal ``#`` —
        # otherwise a valid command falsely fails the handoff with
        # PROFILE_VALIDATE_TOOLCHAIN_UNPROVISIONED.
        targets = validate_command_probe_targets(
            _profile_with_validate(["# run lint\nruff check .", "mypy src"])
        )
        assert [(t.tool, t.command) for t in targets] == [
            ("ruff", "# run lint\nruff check ."),
            ("mypy", "mypy src"),
        ]

    def test_probes_tool_after_leading_shell_guard(self) -> None:
        # A required validate command guarded by a leading ``set -e`` (or
        # ``set -euo pipefail``) runs the real tool after the guard under
        # ``sh -lc``; the probe must look past the guard so a missing toolchain
        # is caught at handoff as PROFILE_VALIDATE_TOOLCHAIN_UNPROVISIONED rather
        # than slipping through to fail later in ``monitoring_pr``. The full
        # command is kept as the representative for the operator message.
        targets = validate_command_probe_targets(
            _profile_with_validate(["set -euo pipefail; ruff check .", "mypy src"])
        )
        assert [(t.tool, t.command) for t in targets] == [
            ("ruff", "set -euo pipefail; ruff check ."),
            ("mypy", "mypy src"),
        ]

    def test_skips_guard_only_command_with_no_following_tool(self) -> None:
        # A validate command that is only a shell guard names no tool, so it
        # yields no probe target (fail-open) rather than reporting the guard
        # itself as a missing toolchain.
        targets = validate_command_probe_targets(
            _profile_with_validate(["set -euo pipefail", "ruff check ."])
        )
        assert [(t.tool, t.command) for t in targets] == [("ruff", "ruff check .")]

    def test_skips_advisory_required_false_commands(self) -> None:
        # An advisory (``required: false``) validate command is not probed: its
        # missing tool is recorded non-blocking by the runner, so it must not fail
        # the handoff with PROFILE_VALIDATE_TOOLCHAIN_UNPROVISIONED. Only the
        # required command yields a probe target.
        targets = validate_command_probe_targets(
            _profile_with_validate_objects(
                [
                    {"command": "advisory-lint .", "required": False},
                    {"command": "ruff check ."},
                ]
            )
        )
        assert [(t.tool, t.command) for t in targets] == [("ruff", "ruff check .")]

    def test_probes_post_agent_tools_before_validate(self) -> None:
        # PR-monitor pre-push validation (including at ``sync_base_push``) runs the
        # ``post_agent`` phase before ``validate`` (the ``("post_agent",
        # "validate")`` plan), so a ``post_agent`` tool like ``make`` whose setup
        # did not install must be probed too — otherwise the missing tool slips
        # past the handoff and dies 127 later during pre-push validation instead of
        # failing early with PROFILE_VALIDATE_TOOLCHAIN_UNPROVISIONED. ``post_agent``
        # targets come first, matching runtime execution order.
        targets = validate_command_probe_targets(
            _profile_with_post_agent_and_validate(
                post_agent=["make build-assets"],
                validate=["ruff check ."],
            )
        )
        assert [(t.tool, t.command) for t in targets] == [
            ("make", "make build-assets"),
            ("ruff", "ruff check ."),
        ]

    def test_probes_post_agent_then_refresh_then_validate_in_runtime_order(self) -> None:
        # The full pre-push execution order is ``post_agent`` -> ``db_refresh``
        # (pre_validation_refresh) -> ``validate``; the probe targets follow that
        # order so the operator message names a representative command from the
        # phase a missing tool actually runs in.
        targets = validate_command_probe_targets(
            _profile_with_post_agent_and_validate(
                post_agent=["make build-assets"],
                pre_validation_refresh=["alembic upgrade head"],
                validate=["ruff check ."],
            )
        )
        assert [(t.tool, t.command) for t in targets] == [
            ("make", "make build-assets"),
            ("alembic", "alembic upgrade head"),
            ("ruff", "ruff check ."),
        ]

    def test_dedupes_post_agent_tool_shared_with_validate(self) -> None:
        # A tool that appears in both a ``post_agent`` command and a validate
        # command collapses to a single probe target, keeping the first
        # (``post_agent``) command as the representative for the operator message.
        targets = validate_command_probe_targets(
            _profile_with_post_agent_and_validate(
                post_agent=["python -m build"],
                validate=["python -m pytest -q"],
            )
        )
        assert [(t.tool, t.command) for t in targets] == [
            ("python", "python -m build"),
        ]

    def test_skips_advisory_required_false_post_agent_commands(self) -> None:
        # An advisory (``required: false``) ``post_agent`` command is non-blocking
        # in the runner (its non-zero/127 result does not fail validation), so it
        # must not fail the handoff with PROFILE_VALIDATE_TOOLCHAIN_UNPROVISIONED.
        # Only the required validate command yields a probe target.
        targets = validate_command_probe_targets(
            _profile_with_post_agent_and_validate(
                post_agent=[{"command": "make build-assets", "required": False}],
                validate=["ruff check ."],
            )
        )
        assert [(t.tool, t.command) for t in targets] == [("ruff", "ruff check .")]

    def test_skips_unprobeable_post_agent_commands(self) -> None:
        # A ``post_agent`` command whose leading token is un-probeable (a ``cd``
        # that changes the resolving directory) fails open like any other command
        # rather than reporting a false missing toolchain.
        targets = validate_command_probe_targets(
            _profile_with_post_agent_and_validate(
                post_agent=["cd build"],
                validate=["ruff check ."],
            )
        )
        assert [(t.tool, t.command) for t in targets] == [("ruff", "ruff check .")]

    def test_probes_pre_validation_refresh_tools_before_validate(self) -> None:
        # ``profile_phase_command_plan`` prepends ``database.pre_validation_refresh``
        # commands as required DB-refresh gates whenever the validate phase runs, so
        # a refresh hook like ``alembic upgrade head`` whose tool setup did not
        # install must be probed too — otherwise the missing tool slips past the
        # handoff and dies 127 later during pre-push validation. Refresh targets
        # come first, matching runtime execution order.
        targets = validate_command_probe_targets(
            _profile_with_refresh_and_validate(
                pre_validation_refresh=["alembic upgrade head"],
                validate=["ruff check ."],
            )
        )
        assert [(t.tool, t.command) for t in targets] == [
            ("alembic", "alembic upgrade head"),
            ("ruff", "ruff check ."),
        ]

    def test_dedupes_refresh_tool_shared_with_validate(self) -> None:
        # A tool that appears in both a refresh hook and a validate command
        # collapses to a single probe target, keeping the first (refresh) command
        # as the representative for the operator message.
        targets = validate_command_probe_targets(
            _profile_with_refresh_and_validate(
                pre_validation_refresh=["python -m alembic upgrade head"],
                validate=["python -m pytest -q"],
            )
        )
        assert [(t.tool, t.command) for t in targets] == [
            ("python", "python -m alembic upgrade head"),
        ]

    def test_skips_advisory_required_false_refresh_commands(self) -> None:
        # An advisory (``required: false``) refresh hook is non-blocking in the
        # runner, so it must not fail the handoff with
        # PROFILE_VALIDATE_TOOLCHAIN_UNPROVISIONED. Only the required validate
        # command yields a probe target.
        targets = validate_command_probe_targets(
            _profile_with_refresh_and_validate(
                pre_validation_refresh=[{"command": "alembic upgrade head", "required": False}],
                validate=["ruff check ."],
            )
        )
        assert [(t.tool, t.command) for t in targets] == [("ruff", "ruff check .")]

    def test_skips_unprobeable_refresh_commands(self) -> None:
        # A refresh hook whose leading token is un-probeable (a ``psql`` heredoc
        # guarded by ``cd``) fails open like any other validate command rather than
        # reporting a false missing toolchain.
        targets = validate_command_probe_targets(
            _profile_with_refresh_and_validate(
                pre_validation_refresh=["cd db"],
                validate=["ruff check ."],
            )
        )
        assert [(t.tool, t.command) for t in targets] == [("ruff", "ruff check .")]
