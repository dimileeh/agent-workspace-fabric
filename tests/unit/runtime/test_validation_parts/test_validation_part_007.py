"""Browser phase command planning tests.

Split from ``test_validation_part_003.py`` to keep each first-party test file
under the maintainability line limit.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.common.commands import FakeCommandRunner
from awf.profiles.models import WorkspaceProfile
from awf.runtime.validation import ValidationRunner, profile_phase_command_plan

_COMPOSE_PROJECT = "awf_ws_val"
_COMPOSE_FILE = Path("/fake/compose.yml")


@pytest.fixture
def runner(tmp_path: Path) -> tuple[FakeCommandRunner, ValidationRunner]:
    fake = FakeCommandRunner()
    return fake, ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts")


class TestBrowserPhaseCommandPlan:
    @pytest.mark.unit
    def test_profile_phase_command_plan_adds_browser_install_after_setup_dependency_when_batched(
        self,
    ) -> None:
        profile = WorkspaceProfile.model_validate(
            {
                "name": "browser-setup-test",
                "runtime": {"browsers": ["chromium"]},
                "phases": {
                    "setup": ["npm install", "node scripts/setup-playwright.js"],
                    "pre_agent": ["node scripts/pre.js"],
                },
                "database": {"generated_setup": ["python scripts/db_generated_setup.py"]},
            }
        )

        commands = profile_phase_command_plan(profile, ("setup", "pre_agent"))

        assert [(command.phase, command.command.command) for command in commands] == [
            ("setup", "npm install"),
            ("setup", "npx playwright install chromium"),
            ("setup", "node scripts/setup-playwright.js"),
            ("db_generated_setup", "python scripts/db_generated_setup.py"),
            ("pre_agent", "node scripts/pre.js"),
        ]
        assert commands[1].command.required is False

    @pytest.mark.unit
    def test_profile_phase_command_plan_root_install_satisfies_scoped_browser_install(
        self,
    ) -> None:
        profile = WorkspaceProfile.model_validate(
            {
                "name": "browser-scoped-validation-root-install-test",
                "runtime": {"browsers": ["chromium"]},
                "phases": {
                    "setup": [
                        "pnpm install --frozen-lockfile",
                        "pnpm --filter web exec playwright test --project=setup",
                    ],
                    "pre_agent": ["node scripts/pre.js"],
                    "validate": ["pnpm --filter web test:e2e"],
                },
            }
        )

        commands = profile_phase_command_plan(profile, ("setup", "pre_agent"))

        assert [(command.phase, command.command.command) for command in commands] == [
            ("setup", "pnpm install --frozen-lockfile"),
            ("setup", "pnpm --filter web exec playwright install chromium"),
            ("setup", "pnpm --filter web exec playwright test --project=setup"),
            ("pre_agent", "node scripts/pre.js"),
        ]
        assert commands[1].command.required is False

    @pytest.mark.unit
    def test_profile_phase_command_plan_splits_setup_install_chain_before_browser_work(
        self,
    ) -> None:
        profile = WorkspaceProfile.model_validate(
            {
                "name": "browser-setup-chain-test",
                "runtime": {"browsers": ["chromium"]},
                "phases": {
                    "setup": ["pnpm install && pnpm exec playwright test --project=setup"],
                },
            }
        )

        commands = profile_phase_command_plan(profile, ("setup",))

        assert [(command.phase, command.command.command) for command in commands] == [
            ("setup", "pnpm install"),
            ("setup", "pnpm exec playwright install chromium"),
            ("setup", "pnpm exec playwright test --project=setup"),
        ]
        assert commands[1].command.required is False

    @pytest.mark.unit
    def test_profile_phase_command_plan_splits_semicolon_install_chain_before_browser_work(
        self,
    ) -> None:
        profile = WorkspaceProfile.model_validate(
            {
                "name": "browser-setup-semicolon-chain-test",
                "runtime": {"browsers": ["chromium"]},
                "phases": {
                    "setup": ["set -e; pnpm install; pnpm exec playwright test --project=setup"],
                },
            }
        )

        commands = profile_phase_command_plan(profile, ("setup",))

        assert [(command.phase, command.command.command) for command in commands] == [
            ("setup", "set -e; pnpm install"),
            ("setup", "pnpm exec playwright install chromium"),
            ("setup", "set -e && pnpm exec playwright test --project=setup"),
        ]
        assert commands[1].command.required is False

    @pytest.mark.unit
    def test_profile_phase_command_plan_splits_pre_agent_install_chain_after_preamble(
        self,
    ) -> None:
        profile = WorkspaceProfile.model_validate(
            {
                "name": "browser-pre-agent-chain-test",
                "runtime": {"browsers": ["chromium"]},
                "phases": {
                    "setup": ["node scripts/generate-config.js"],
                    "pre_agent": ["corepack enable && pnpm install && pnpm exec playwright test"],
                },
            }
        )

        commands = profile_phase_command_plan(profile, ("setup", "pre_agent"))

        assert [(command.phase, command.command.command) for command in commands] == [
            ("setup", "node scripts/generate-config.js"),
            ("pre_agent", "corepack enable && pnpm install"),
            ("setup", "pnpm exec playwright install chromium"),
            ("pre_agent", "pnpm exec playwright test"),
        ]
        assert commands[2].command.required is False

    @pytest.mark.unit
    def test_profile_phase_command_plan_uses_pre_agent_dependency_install_for_browser_install(
        self,
    ) -> None:
        profile = WorkspaceProfile.model_validate(
            {
                "name": "browser-pre-agent-install-test",
                "runtime": {"browsers": ["chromium"]},
                "phases": {
                    "setup": ["node scripts/generate-config.js"],
                    "pre_agent": ["pnpm install --frozen-lockfile"],
                },
            }
        )

        commands = profile_phase_command_plan(profile, ("setup", "pre_agent"))

        assert [(command.phase, command.command.command) for command in commands] == [
            ("setup", "node scripts/generate-config.js"),
            ("pre_agent", "pnpm install --frozen-lockfile"),
            ("setup", "pnpm exec playwright install chromium"),
        ]
        assert commands[2].command.required is False

    @pytest.mark.unit
    def test_profile_phase_command_plan_defers_browser_install_until_validate_install(
        self,
    ) -> None:
        profile = WorkspaceProfile.model_validate(
            {
                "name": "browser-validate-install-test",
                "runtime": {"browsers": ["chromium"]},
                "phases": {
                    "setup": ["node scripts/generate-config.js"],
                    "validate": [
                        "pnpm install --frozen-lockfile",
                        "pnpm test",
                    ],
                },
            }
        )

        commands = profile_phase_command_plan(profile, ("setup", "validate"))

        assert [(command.phase, command.command.command) for command in commands] == [
            ("setup", "node scripts/generate-config.js"),
            ("validate", "pnpm install --frozen-lockfile"),
            ("setup", "pnpm exec playwright install chromium"),
            ("validate", "pnpm test"),
        ]
        assert commands[2].command.required is False

    @pytest.mark.unit
    def test_profile_phase_command_plan_defers_browser_install_until_matching_validate_install(
        self,
    ) -> None:
        profile = WorkspaceProfile.model_validate(
            {
                "name": "browser-mixed-validate-install-test",
                "runtime": {"browsers": ["chromium"]},
                "phases": {
                    "validate": [
                        "npm install",
                        "pnpm -C web install --frozen-lockfile",
                        "pnpm -C web test:e2e",
                    ],
                },
            }
        )

        commands = profile_phase_command_plan(profile, ("validate",))

        assert [(command.phase, command.command.command) for command in commands] == [
            ("validate", "npm install"),
            ("validate", "pnpm -C web install --frozen-lockfile"),
            ("setup", "pnpm -C web exec playwright install chromium"),
            ("validate", "pnpm -C web test:e2e"),
        ]
        assert commands[2].command.required is False

    @pytest.mark.unit
    def test_profile_phase_command_plan_appends_deferred_browser_install_without_matching_validate_install(
        self,
    ) -> None:
        profile = WorkspaceProfile.model_validate(
            {
                "name": "browser-unmatched-validate-install-test",
                "runtime": {"browsers": ["chromium"]},
                "phases": {
                    "validate": [
                        "npm install",
                        "pnpm --filter web test:e2e",
                    ],
                },
            }
        )

        commands = profile_phase_command_plan(profile, ("validate",))

        assert [(command.phase, command.command.command) for command in commands] == [
            ("validate", "npm install"),
            ("setup", "pnpm --filter web exec playwright install chromium"),
            ("validate", "pnpm --filter web test:e2e"),
        ]
        assert commands[1].command.required is False

    @pytest.mark.unit
    def test_profile_phase_command_plan_validate_only_adds_browser_install_without_dependency_install(
        self,
    ) -> None:
        profile = WorkspaceProfile.model_validate(
            {
                "name": "browser-validate-without-install-test",
                "runtime": {"browsers": ["chromium"]},
                "phases": {
                    "validate": [
                        "pnpm test:e2e",
                    ],
                },
            }
        )

        commands = profile_phase_command_plan(profile, ("validate",))

        assert [(command.phase, command.command.command) for command in commands] == [
            ("setup", "pnpm exec playwright install chromium"),
            ("validate", "pnpm test:e2e"),
        ]
        assert commands[0].command.required is False

    @pytest.mark.unit
    def test_profile_phase_command_plan_defers_browser_install_across_production_batches(
        self,
    ) -> None:
        profile = WorkspaceProfile.model_validate(
            {
                "name": "browser-split-validate-install-test",
                "runtime": {"browsers": ["chromium"]},
                "phases": {
                    "setup": ["node scripts/generate-config.js"],
                    "pre_agent": ["node scripts/pre.js"],
                    "post_agent": ["ruff format --check"],
                    "validate": [
                        "pnpm install --frozen-lockfile",
                        "pnpm test",
                    ],
                },
            }
        )

        setup_commands = profile_phase_command_plan(profile, ("setup", "pre_agent"))
        validate_commands = profile_phase_command_plan(profile, ("post_agent", "validate"))

        assert [(command.phase, command.command.command) for command in setup_commands] == [
            ("setup", "node scripts/generate-config.js"),
            ("pre_agent", "node scripts/pre.js"),
        ]
        assert [(command.phase, command.command.command) for command in validate_commands] == [
            ("post_agent", "ruff format --check"),
            ("validate", "pnpm install --frozen-lockfile"),
            ("setup", "pnpm exec playwright install chromium"),
            ("validate", "pnpm test"),
        ]
        assert validate_commands[2].command.required is False

    @pytest.mark.unit
    def test_profile_phase_command_plan_installs_browser_before_pre_agent_browser_use(
        self,
    ) -> None:
        profile = WorkspaceProfile.model_validate(
            {
                "name": "browser-pre-agent-preinstalled-deps-test",
                "runtime": {"browsers": ["chromium"]},
                "phases": {
                    "pre_agent": ["npx playwright test --project=setup"],
                    "validate": ["npm install", "npm test"],
                },
            }
        )

        commands = profile_phase_command_plan(profile, ("setup", "pre_agent"))

        assert [(command.phase, command.command.command) for command in commands] == [
            ("setup", "npx playwright install chromium"),
            ("pre_agent", "npx playwright test --project=setup"),
        ]
        assert commands[0].command.required is False

    @pytest.mark.unit
    def test_profile_phase_command_plan_defers_browser_install_past_unrelated_split_install(
        self,
    ) -> None:
        profile = WorkspaceProfile.model_validate(
            {
                "name": "browser-split-unrelated-install-test",
                "runtime": {"browsers": ["chromium"]},
                "phases": {
                    "setup": ["npm install"],
                    "pre_agent": ["node scripts/pre.js"],
                    "validate": [
                        "pnpm -C web install --frozen-lockfile",
                        "pnpm -C web test:e2e",
                    ],
                },
            }
        )

        setup_commands = profile_phase_command_plan(profile, ("setup", "pre_agent"))
        validate_commands = profile_phase_command_plan(profile, ("validate",))

        assert [(command.phase, command.command.command) for command in setup_commands] == [
            ("setup", "npm install"),
            ("pre_agent", "node scripts/pre.js"),
        ]
        assert [(command.phase, command.command.command) for command in validate_commands] == [
            ("validate", "pnpm -C web install --frozen-lockfile"),
            ("setup", "pnpm -C web exec playwright install chromium"),
            ("validate", "pnpm -C web test:e2e"),
        ]
        assert validate_commands[1].command.required is False

    @pytest.mark.unit
    def test_profile_phase_command_plan_installs_browsers_after_post_agent_install(
        self,
    ) -> None:
        profile = WorkspaceProfile.model_validate(
            {
                "name": "browser-post-agent-install-test",
                "runtime": {"browsers": ["chromium"]},
                "phases": {
                    "setup": ["node scripts/generate-config.js"],
                    "pre_agent": ["node scripts/pre.js"],
                    "post_agent": ["pnpm install --frozen-lockfile"],
                    "validate": ["pnpm test:e2e"],
                },
            }
        )

        setup_commands = profile_phase_command_plan(profile, ("setup", "pre_agent"))
        validate_commands = profile_phase_command_plan(profile, ("post_agent", "validate"))

        assert [(command.phase, command.command.command) for command in setup_commands] == [
            ("setup", "node scripts/generate-config.js"),
            ("pre_agent", "node scripts/pre.js"),
        ]
        assert [(command.phase, command.command.command) for command in validate_commands] == [
            ("post_agent", "pnpm install --frozen-lockfile"),
            ("setup", "pnpm exec playwright install chromium"),
            ("validate", "pnpm test:e2e"),
        ]
        assert validate_commands[1].command.required is False

    @pytest.mark.unit
    def test_profile_phase_command_plan_uses_post_agent_install_before_post_agent_browser_use_with_validate_install(
        self,
    ) -> None:
        profile = WorkspaceProfile.model_validate(
            {
                "name": "browser-post-agent-and-validate-install-test",
                "runtime": {"browsers": ["chromium"]},
                "phases": {
                    "post_agent": [
                        "pnpm install",
                        "pnpm exec playwright test --project=setup",
                    ],
                    "validate": ["pnpm install", "pnpm test:e2e"],
                },
            }
        )

        commands = profile_phase_command_plan(profile, ("post_agent", "validate"))

        assert [(command.phase, command.command.command) for command in commands] == [
            ("post_agent", "pnpm install"),
            ("setup", "pnpm exec playwright install chromium"),
            ("post_agent", "pnpm exec playwright test --project=setup"),
            ("validate", "pnpm install"),
            ("validate", "pnpm test:e2e"),
        ]
        assert commands[1].command.required is False

    @pytest.mark.unit
    def test_profile_phase_command_plan_preserves_pre_install_validate_commands(
        self,
    ) -> None:
        profile = WorkspaceProfile.model_validate(
            {
                "name": "browser-validate-install-reordered-test",
                "runtime": {"browsers": ["chromium"]},
                "phases": {
                    "setup": ["node scripts/generate-config.js"],
                    "validate": [
                        "pnpm test:e2e",
                        "pnpm install --frozen-lockfile",
                        "pnpm test:unit",
                    ],
                },
            }
        )

        commands = profile_phase_command_plan(profile, ("setup", "validate"))

        assert [(command.phase, command.command.command) for command in commands] == [
            ("setup", "node scripts/generate-config.js"),
            ("validate", "pnpm test:e2e"),
            ("validate", "pnpm install --frozen-lockfile"),
            ("setup", "pnpm exec playwright install chromium"),
            ("validate", "pnpm test:unit"),
        ]
        assert commands[3].command.required is False

    @pytest.mark.unit
    def test_profile_phase_command_plan_adds_one_browser_install_for_multiple_browsers(
        self,
    ) -> None:
        profile = WorkspaceProfile.model_validate(
            {
                "name": "browser-setup-test",
                "runtime": {"browsers": ["firefox", "CHROMIUM", "firefox"]},
                "phases": {"setup": ["npm install"]},
            }
        )

        commands = profile_phase_command_plan(profile, ("setup",))

        assert [command.command.command for command in commands] == [
            "npm install",
            "npx playwright install firefox chromium",
        ]

    @pytest.mark.unit
    def test_profile_phase_command_plan_empty_browsers_preserves_non_web_setup(self) -> None:
        profile = WorkspaceProfile.model_validate(
            {
                "name": "plain-setup-test",
                "runtime": {"browsers": []},
                "phases": {"setup": ["python scripts/setup.py"]},
            }
        )

        commands = profile_phase_command_plan(profile, ("setup",))

        assert [(command.phase, command.command.command) for command in commands] == [
            ("setup", "python scripts/setup.py"),
        ]

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("setup_command", "expected"),
        [
            ("npm install", "npx playwright install chromium"),
            ("pnpm install --frozen-lockfile", "pnpm exec playwright install chromium"),
            ("yarn install --frozen-lockfile", "yarn playwright install chromium"),
            ("bun install --frozen-lockfile", "bunx playwright install chromium"),
        ],
    )
    def test_profile_phase_command_plan_browser_install_tracks_package_manager(
        self,
        setup_command: str,
        expected: str,
    ) -> None:
        profile = WorkspaceProfile.model_validate(
            {
                "name": "browser-setup-test",
                "runtime": {"browsers": ["chromium"]},
                "phases": {"setup": [setup_command]},
            }
        )

        commands = profile_phase_command_plan(profile, ("setup",))

        assert commands[-1].command.command == expected

    @pytest.mark.unit
    def test_profile_phase_command_plan_browser_install_prefers_dependency_install_runner(
        self,
    ) -> None:
        profile = WorkspaceProfile.model_validate(
            {
                "name": "browser-setup-test",
                "runtime": {"browsers": ["chromium"]},
                "phases": {
                    "setup": [
                        "npm run generate-schema",
                        "pnpm install --frozen-lockfile",
                    ]
                },
            }
        )

        commands = profile_phase_command_plan(profile, ("setup",))

        assert commands[-1].command.command == "pnpm exec playwright install chromium"

    @pytest.mark.unit
    def test_profile_phase_command_plan_browser_install_uses_generated_setup_package_manager(
        self,
    ) -> None:
        profile = WorkspaceProfile.model_validate(
            {
                "name": "browser-generated-setup-test",
                "runtime": {"browsers": ["chromium"]},
                "phases": {"setup": ["python scripts/setup.py"]},
                "database": {"generated_setup": ["pnpm install --frozen-lockfile"]},
            }
        )

        commands = profile_phase_command_plan(profile, ("setup",))

        assert [(command.phase, command.command.command) for command in commands] == [
            ("setup", "python scripts/setup.py"),
            ("db_generated_setup", "pnpm install --frozen-lockfile"),
            ("setup", "pnpm exec playwright install chromium"),
        ]

    @pytest.mark.unit
    async def test_generated_browser_install_failure_is_non_blocking(
        self, runner: tuple[FakeCommandRunner, ValidationRunner]
    ) -> None:
        fake, val = runner
        fake.queue_result(returncode=0, stdout="dependencies installed")
        fake.queue_result(returncode=1, stderr="browser download failed")
        fake.queue_result(returncode=0, stdout="pre-agent ok")
        profile = WorkspaceProfile.model_validate(
            {
                "name": "browser-setup-test",
                "runtime": {"browsers": ["chromium"]},
                "phases": {"setup": ["npm install"], "pre_agent": ["node scripts/pre.js"]},
            }
        )

        result = await val.run_profile_phases(
            workspace_id="ws_browser_install",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            profile=profile,
            phase_names=("setup", "pre_agent"),
        )

        assert result.all_passed
        assert result.first_failure is None
        assert len(fake.calls) == 3
        browser_install = result.commands[1]
        assert browser_install.command == "npx playwright install chromium"
        assert browser_install.returncode == 1
        assert browser_install.required is False
