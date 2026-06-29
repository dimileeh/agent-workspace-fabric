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

    @pytest.mark.parametrize(
        "setup_command",
        ["yarn workspaces focus --all", "yarn workspaces focus -A"],
    )
    def test_recognizes_yarn_focus_all_as_project_install(self, setup_command: str) -> None:
        profile = _profile_with_setup_validate_and_browsers(
            setup=[],
            validate=[f"{setup_command} && yarn workspace web playwright test"],
        )

        assert browser_probe_workdir(profile) == "/workspace"

        commands = profile_phase_command_plan(profile, ["setup", "validate"])

        assert [(command.phase, command.command.command) for command in commands] == [
            ("validate", setup_command),
            ("setup", "yarn workspace web playwright install chromium"),
            ("validate", "yarn workspace web playwright test"),
        ]

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

    def test_pytest_only_python_playwright_profile_uses_python_browser_install(
        self,
    ) -> None:
        profile = _profile_with_setup_validate_and_browsers(
            setup=[],
            validate=["pytest --browser chromium"],
        )

        command = playwright_browser_install_command(profile)

        assert command is not None
        assert command.command == "python -m playwright install chromium"
        assert command.required is False

    @pytest.mark.parametrize(
        ("validate_command", "expected_command"),
        [
            (
                "uv run pytest --browser chromium",
                "uv run -m playwright install chromium",
            ),
            (
                "uv run -m pytest --browser chromium",
                "uv run -m playwright install chromium",
            ),
            (
                "uv run --module pytest --browser chromium",
                "uv run -m playwright install chromium",
            ),
            (
                "uv run --python 3.12 --extra dev pytest --browser chromium",
                "uv run --python 3.12 --extra dev -m playwright install chromium",
            ),
        ],
    )
    def test_uv_run_pytest_only_python_playwright_profile_uses_uv_browser_install(
        self,
        validate_command: str,
        expected_command: str,
    ) -> None:
        profile = _profile_with_setup_validate_and_browsers(
            setup=[],
            validate=[validate_command],
        )

        command = playwright_browser_install_command(profile)

        assert command is not None
        assert command.command == expected_command
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
            (
                "uv pip install --python .venv playwright",
                ".venv/bin/python -m playwright install chromium",
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
        ("setup_command", "expected_command"),
        [
            (
                "cd apps/web && . .venv/bin/activate && pip install playwright",
                "cd apps/web && .venv/bin/python -m playwright install chromium",
            ),
            (
                "cd apps/web && source .venv/bin/activate && python -m pip install playwright",
                "cd apps/web && .venv/bin/python -m playwright install chromium",
            ),
            (
                "cd apps/web && . .venv/bin/activate && pip3.12 install playwright",
                "cd apps/web && .venv/bin/python3.12 -m playwright install chromium",
            ),
        ],
    )
    def test_python_playwright_profile_preserves_cd_scope_after_venv_activation(
        self,
        setup_command: str,
        expected_command: str,
    ) -> None:
        profile = _profile_with_setup_validate_and_browsers(
            setup=[setup_command],
            validate=["cd apps/web && . .venv/bin/activate && pytest --browser chromium"],
        )

        command = playwright_browser_install_command(profile)

        assert command is not None
        assert command.command == expected_command
        assert command.required is False

    def test_python_playwright_profile_ignores_non_venv_source_before_scoped_pip(
        self,
    ) -> None:
        profile = _profile_with_setup_validate_and_browsers(
            setup=["cd apps/web && . .env && pip install playwright"],
            validate=["cd apps/web && pytest --browser chromium"],
        )

        command = playwright_browser_install_command(profile)

        assert command is not None
        assert command.command == "cd apps/web && python -m playwright install chromium"
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

    @pytest.mark.parametrize(
        "setup_command",
        [
            "python -m pip install -e .[e2e]",
            "pip install .[e2e]",
        ],
    )
    def test_python_playwright_profile_detects_pip_local_project_extra(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        setup_command: str,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text(
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

    def test_python_playwright_profile_maps_container_workspace_cd_scope(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace_root = tmp_path / "workspace"
        project_root = workspace_root / "apps" / "web"
        host_root = tmp_path / "host"
        project_root.mkdir(parents=True)
        host_root.mkdir()
        monkeypatch.chdir(host_root)
        (project_root / "requirements.txt").write_text("playwright\n", encoding="utf-8")
        profile = _profile_with_setup_validate_and_browsers(
            setup=["cd /workspace/apps/web && python -m pip install -r requirements.txt"],
            validate=["pytest --browser chromium"],
        )

        command = playwright_browser_install_command(profile, workspace_root=workspace_root)

        assert command is not None
        assert command.command == "cd /workspace/apps/web && python -m playwright install chromium"
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

    def test_node_playwright_usage_defers_to_validate_node_install(self) -> None:
        profile = _profile_with_setup_validate_and_browsers(
            setup=["pnpm exec playwright test"],
            validate=["pnpm install --frozen-lockfile"],
        )

        assert runtime_browser_probe_deferred_until_validate(profile)

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

    def test_browser_install_ignores_global_package_manager_bootstrap(
        self,
    ) -> None:
        profile = _profile_with_setup_and_browsers(
            ["npm install -g pnpm && pnpm install && pnpm exec playwright test"]
        )

        commands = profile_phase_command_plan(profile, ["setup"])

        assert [(command.phase, command.command.command) for command in commands] == [
            ("setup", "npm install -g pnpm && pnpm install"),
            ("setup", "pnpm exec playwright install chromium"),
            ("setup", "pnpm exec playwright test"),
        ]

    def test_deferred_browser_install_stays_before_unsplittable_validate_test(
        self,
    ) -> None:
        profile = _profile_with_setup_validate_and_browsers(
            setup=[],
            validate=["source .env; pnpm install; pnpm exec playwright test"],
        )

        commands = profile_phase_command_plan(profile, ["setup", "validate"])

        assert [(command.phase, command.command.command) for command in commands] == [
            (
                "validate",
                "source .env; pnpm install; "
                "pnpm exec playwright install chromium; pnpm exec playwright test",
            ),
        ]

    def test_deferred_browser_install_is_advisory_inside_unsplittable_and_chain(
        self,
    ) -> None:
        profile = _profile_with_setup_validate_and_browsers(
            setup=[],
            validate=["source .env && pnpm install && pnpm exec playwright test"],
        )

        commands = profile_phase_command_plan(profile, ["setup", "validate"])

        assert [(command.phase, command.command.command) for command in commands] == [
            (
                "validate",
                "source .env && pnpm install && "
                "{ pnpm exec playwright install chromium; pnpm exec playwright test; }",
            ),
        ]

    @pytest.mark.parametrize(
        ("validate_command", "expected"),
        [
            ("pnpx playwright test", "pnpx playwright install chromium"),
            ("bunx playwright test", "bunx playwright install chromium"),
        ],
    )
    def test_browser_install_preserves_direct_playwright_runner_from_validate(
        self,
        validate_command: str,
        expected: str,
    ) -> None:
        profile = _profile_with_setup_validate_and_browsers(
            setup=[],
            validate=[validate_command],
        )

        command = playwright_browser_install_command(profile)

        assert command is not None
        assert command.command == expected

    @pytest.mark.parametrize(
        ("validate_command", "expected"),
        [
            ("pnpm test:e2e", "pnpm exec playwright install chromium"),
            ("bun test:e2e", "bunx playwright install chromium"),
            ("npm run test:e2e", "npx playwright install chromium"),
        ],
    )
    def test_browser_install_infers_manager_from_unscoped_browser_script(
        self,
        validate_command: str,
        expected: str,
    ) -> None:
        profile = _profile_with_setup_validate_and_browsers(
            setup=[],
            validate=[validate_command],
        )

        command = playwright_browser_install_command(profile)

        assert command is not None
        assert command.command == expected

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

    @pytest.mark.parametrize(
        "package_manager",
        [
            'pnpm -C "$APP_DIR"',
            'npm --prefix "$APP_DIR"',
            "yarn --cwd=`$PROJECT_DIR`",
        ],
    )
    def test_package_manager_location_rejects_expanding_directory_scope(
        self,
        package_manager: str,
    ) -> None:
        assert _node_package_manager_package_dir(package_manager) is None

    def test_package_manager_location_ignores_non_location_equals_option(self) -> None:
        assert _node_package_manager_package_dir("npm --cache=.npm-cache") is None

    def test_dependency_install_parser_skips_incomplete_location_flag(self) -> None:
        assert _node_dependency_install_package_manager("npm --prefix") is None

    def test_dependency_install_parser_ignores_unpreserved_equals_option(self) -> None:
        assert _node_dependency_install_package_manager("npm --userconfig=.npmrc ci") == "npm"

    @pytest.mark.parametrize(
        "command",
        [
            "npm install -g pnpm",
            "npm install --global pnpm",
        ],
    )
    def test_dependency_install_parser_rejects_global_package_manager_bootstrap(
        self,
        command: str,
    ) -> None:
        assert _node_dependency_install_package_manager(command) is None

    @pytest.mark.parametrize(
        "command",
        [
            'pnpm -C "$APP_DIR" install',
            'npm --prefix "$APP_DIR" install',
            "yarn --cwd `$PROJECT_DIR` install",
        ],
    )
    def test_dependency_install_parser_rejects_expanding_directory_scope(
        self,
        command: str,
    ) -> None:
        assert _node_dependency_install_package_manager(command) is None

    @pytest.mark.parametrize(
        ("setup_command", "expected"),
        [
            ('pnpm -C "$APP_DIR" install', "pnpm exec playwright install chromium"),
            ('npm --prefix "$APP_DIR" install', "npx playwright install chromium"),
        ],
    )
    def test_browser_install_does_not_preserve_expanding_directory_scope(
        self,
        setup_command: str,
        expected: str,
    ) -> None:
        command = playwright_browser_install_command(
            _profile_with_setup_and_browsers([setup_command])
        )

        assert command is not None
        assert command.command == expected

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
