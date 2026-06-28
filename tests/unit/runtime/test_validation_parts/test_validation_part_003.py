"""ValidationRunner tests with FakeCommandRunner (no docker needed)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from awf.common.commands import CommandResult, FakeCommandRunner
from awf.profiles.models import ProfileHealthCheck, WorkspaceProfile
from awf.runtime.logs import CommandLogSinks, LogStore
from awf.runtime.validation import (
    ValidationCommandResult,
    ValidationRunner,
    _healthcheck_cli_args,
    _healthcheck_failure_reason,
    profile_phase_command_plan,
)

_COMPOSE_PROJECT = "awf_ws_val"
_COMPOSE_FILE = Path("/fake/compose.yml")


def _uv_pypi_dns_failure(*, package: str = "docker==7.1.0") -> str:
    return f"""
  x Failed to download `{package}`
  |- Failed to fetch: `https://files.pythonhosted.org/packages/aa/bb/docker-7.1.0.whl`
  |- Request failed after 3 retries
  |- error sending request for url (https://files.pythonhosted.org/packages/aa/bb/docker-7.1.0.whl)
  `- client error (Connect): dns error: failed to lookup address information: No address associated with hostname
""".strip()


class _CountingLogStore(LogStore):
    def __init__(self, *, root: Path) -> None:
        super().__init__(root=root)
        self.open_command_stream_calls: list[str] = []

    async def open_command_streams(
        self,
        *,
        workspace_id: str,
        base_stream_id: str,
        source: str,
        name: str,
    ) -> CommandLogSinks:
        self.open_command_stream_calls.append(base_stream_id)
        return await super().open_command_streams(
            workspace_id=workspace_id,
            base_stream_id=base_stream_id,
            source=source,
            name=name,
        )


@pytest.fixture
def runner(tmp_path: Path) -> tuple[FakeCommandRunner, ValidationRunner]:
    fake = FakeCommandRunner()
    return fake, ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts")


def _identity_profile(**overrides: object) -> WorkspaceProfile:
    body: dict[str, object] = {
        "name": "identity-test",
        "version": 3,
        "source": "repo:.awf/workspace.yml",
        "description": "Human-facing profile details are not validation identity.",
        "runtime": {
            "agent_image": "ghcr.io/acme/agent:1",
            "toolchain_image": "ghcr.io/acme/toolchain:1",
            "environment": {
                "PYTHON_VERSION": "3.12",
                "API_TOKEN": "sk-secret-value",
            },
        },
        "docker": {
            "mode": "dind",
            "compose_files": ["compose.yml", "compose.override.yml"],
            "project_directory": ".",
            "startup_timeout_seconds": 120,
        },
        "services": [
            {
                "name": "api",
                "image": "ghcr.io/acme/api:1",
                "environment": {"DATABASE_URL": "postgres://secret", "LOG_LEVEL": "debug"},
                "depends_on": ["postgres"],
                "healthcheck_cmd": "curl -fsS http://api:8000/health",
                "ports": [(8000, 8000)],
            },
            {
                "name": "postgres",
                "image": "postgres:16",
                "environment": {"POSTGRES_PASSWORD": "postgres-secret"},
            },
        ],
        "phases": {
            "validate": ["pytest -q"],
        },
        "validation": {
            "healthchecks": [
                {
                    "name": "api",
                    "command": "curl -fsS http://api:8000/health",
                    "timeout_seconds": 20,
                }
            ],
            "coverage": {
                "minimum_percent": 99,
                "provider": "python",
                "command": "pytest --cov=awf",
            },
            "requested_tier": 2,
            "retry_budget": 1,
        },
        "monitor": {
            "initial_review_grace_period_seconds": 60,
            "non_check_reviewer_settle_seconds": 60,
            "non_check_reviewer_logins": ["greptile-apps"],
        },
        "planning": {
            "required": True,
            "plan_path": "docs/awf-plans/{workspace_id}.md",
            "conformance_report_path": "docs/awf-plans/{workspace_id}.json",
        },
        "security": {
            "egress": {
                "mode": "restricted",
            }
        },
        "secrets": [
            {
                "name": "github-token",
                "target": "GITHUB_TOKEN",
                "kind": "env",
                "provider": "vault",
                "ref": "secret/data/github/token",
            }
        ],
    }
    body.update(overrides)
    return WorkspaceProfile.model_validate(body)


def _identity_profile_with_endpoint(**endpoint_overrides: object) -> WorkspaceProfile:
    endpoint: dict[str, object] = {
        "name": "api",
        "service": "api",
        "port": 8000,
        "path": "/",
        "health": {"path": "/health", "expected_status": 200},
        "visibility": "agent",
    }
    endpoint.update(endpoint_overrides)
    return _identity_profile(app_endpoints=[endpoint])


class _SleepingRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def run(
        self,
        args: list[str],
        *,
        input_bytes: bytes | None = None,
        cwd: str | None = None,
    ) -> CommandResult:
        del input_bytes, cwd
        self.calls.append(list(args))

        if "awf-cleanup" in args:
            return CommandResult(returncode=0, stdout="cleanup ok", stderr="")

        await asyncio.sleep(60)
        return CommandResult(returncode=0, stdout="late", stderr="")


class _ImmediateTimeoutStreamingRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def run(
        self,
        args: list[str],
        *,
        input_bytes: bytes | None = None,
        cwd: str | None = None,
    ) -> CommandResult:
        del input_bytes, cwd
        self.calls.append(list(args))
        if "awf-cleanup" in args:
            return CommandResult(returncode=0, stdout="cleanup ok", stderr="")
        return CommandResult(returncode=0, stdout="", stderr="")

    async def run_streaming(
        self,
        args: list[str],
        *,
        on_stdout: object = None,
        on_stderr: object = None,
        input_bytes: bytes | None = None,
        cwd: str | None = None,
        wall_timeout_seconds: float | None = None,
        idle_timeout_seconds: float | None = None,
    ) -> CommandResult:
        del on_stdout, on_stderr, input_bytes, cwd, wall_timeout_seconds, idle_timeout_seconds
        self.calls.append(list(args))
        raise TimeoutError


class _CancellingRunner:
    def __init__(self) -> None:
        self.cleanup_calls: list[list[str]] = []

    async def run(
        self,
        args: list[str],
        *,
        input_bytes: bytes | None = None,
        cwd: str | None = None,
    ) -> CommandResult:
        del input_bytes, cwd
        if "awf-cleanup" in args:
            self.cleanup_calls.append(list(args))
            return CommandResult(returncode=0, stdout="cleanup ok", stderr="")
        raise asyncio.CancelledError


class _NonStreamingRunner:
    def __init__(self, result: CommandResult) -> None:
        self.result = result
        self.calls: list[list[str]] = []

    async def run(
        self,
        args: list[str],
        *,
        input_bytes: bytes | None = None,
        cwd: str | None = None,
    ) -> CommandResult:
        del input_bytes, cwd
        self.calls.append(list(args))
        return self.result


class _StreamingRunner:
    def __init__(self, result: CommandResult) -> None:
        self.result = result
        self.calls: list[list[str]] = []

    async def run(
        self,
        args: list[str],
        *,
        input_bytes: bytes | None = None,
        cwd: str | None = None,
    ) -> CommandResult:
        del input_bytes, cwd
        self.calls.append(list(args))
        return self.result

    async def run_streaming(
        self,
        args: list[str],
        *,
        on_stdout: object = None,
        on_stderr: object = None,
        input_bytes: bytes | None = None,
        cwd: str | None = None,
        wall_timeout_seconds: float | None = None,
        idle_timeout_seconds: float | None = None,
    ) -> CommandResult:
        del input_bytes, cwd, wall_timeout_seconds, idle_timeout_seconds
        self.calls.append(list(args))
        if on_stdout is not None:
            await on_stdout(self.result.stdout)  # type: ignore[misc]
        if on_stderr is not None:
            await on_stderr(self.result.stderr)  # type: ignore[misc]
        return self.result


class TestHappyPath:
    @pytest.mark.unit
    def test_profile_phase_command_plan_uses_runtime_phase_order(self) -> None:
        profile = WorkspaceProfile.model_validate(
            {
                "name": "phase-order-test",
                "phases": {
                    "setup": ["python scripts/setup.py"],
                    "pre_agent": ["python scripts/pre_agent.py"],
                    "post_agent": ["ruff format --check"],
                    "validate": ["pytest -q"],
                },
                "database": {
                    "generated_setup": ["python scripts/db_generated_setup.py"],
                    "pre_validation_refresh": ["python scripts/db_refresh.py"],
                },
            }
        )

        commands = profile_phase_command_plan(
            profile,
            ("validate", "pre_agent", "post_agent", "setup"),
        )

        assert [(command.phase, command.command.command) for command in commands] == [
            ("setup", "python scripts/setup.py"),
            ("db_generated_setup", "python scripts/db_generated_setup.py"),
            ("pre_agent", "python scripts/pre_agent.py"),
            ("post_agent", "ruff format --check"),
            ("db_refresh", "python scripts/db_refresh.py"),
            ("validate", "pytest -q"),
        ]

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
    def test_profile_phase_command_plan_runs_validate_install_before_browser_tests(
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
            ("validate", "pnpm install --frozen-lockfile"),
            ("setup", "pnpm exec playwright install chromium"),
            ("validate", "pnpm test:e2e"),
            ("validate", "pnpm test:unit"),
        ]
        assert commands[2].command.required is False

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

    @pytest.mark.unit
    async def test_runs_each_test_command_in_order(
        self, runner: tuple[FakeCommandRunner, ValidationRunner]
    ) -> None:
        fake, val = runner
        fake.queue_result(returncode=0, stdout="lint ok")
        fake.queue_result(returncode=0, stdout="tests ok")

        result = await val.run(
            workspace_id="ws_a",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            test_commands=["ruff check .", "pytest -q"],
        )

        assert result.all_passed
        assert len(result.commands) == 2
        assert result.commands[0].command == "ruff check ."
        assert result.commands[1].command == "pytest -q"
        assert result.migration is None

    @pytest.mark.unit
    async def test_artifacts_written_per_command(
        self, runner: tuple[FakeCommandRunner, ValidationRunner], tmp_path: Path
    ) -> None:
        fake, val = runner
        fake.queue_result(returncode=0, stdout="one-stdout", stderr="one-stderr")

        result = await val.run(
            workspace_id="ws_a",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            test_commands=["echo 1"],
        )
        assert result.all_passed

        stdout_path = result.commands[0].stdout_path
        stderr_path = result.commands[0].stderr_path
        assert stdout_path.read_text() == "one-stdout"
        assert stderr_path.read_text() == "one-stderr"
        assert stdout_path.parent.name == "ws_a"
        assert stdout_path.name == "cmd_01.stdout"

    @pytest.mark.unit
    async def test_profile_phase_artifacts_use_per_phase_indices(
        self, runner: tuple[FakeCommandRunner, ValidationRunner]
    ) -> None:
        fake, val = runner
        fake.queue_result(returncode=0, stdout="health ok")
        fake.queue_result(returncode=0, stdout="format ok")
        fake.queue_result(returncode=0, stdout="tests ok")
        profile = WorkspaceProfile.model_validate(
            {
                "name": "phase-label-test",
                "phases": {
                    "post_agent": ["ruff format --check"],
                    "validate": ["pytest -q"],
                },
                "validation": {
                    "healthchecks": [
                        {
                            "name": "api",
                            "command": "curl -fsS http://localhost:8000/health",
                        }
                    ]
                },
            }
        )

        result = await val.run_profile_phases(
            workspace_id="ws_phase_labels",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            profile=profile,
            phase_names=("post_agent", "validate"),
            run_healthchecks=True,
        )

        assert result.all_passed
        assert [command.stdout_path.name for command in result.commands] == [
            "01_healthcheck.stdout",
            "01_post_agent.stdout",
            "01_validate.stdout",
        ]

    @pytest.mark.unit
    async def test_run_profile_phases_inserts_generated_setup_between_setup_and_pre_agent(
        self, runner: tuple[FakeCommandRunner, ValidationRunner]
    ) -> None:
        fake, val = runner
        fake.queue_result(returncode=0, stdout="setup ok")
        fake.queue_result(returncode=0, stdout="generated ok")
        fake.queue_result(returncode=0, stdout="pre-agent ok")
        profile = WorkspaceProfile.model_validate(
            {
                "name": "db-generated-setup-order",
                "phases": {
                    "setup": ["python scripts/setup.py"],
                    "pre_agent": ["python scripts/pre_agent.py"],
                },
                "database": {
                    "generated_setup": [
                        {
                            "command": "python scripts/db_generated_setup.py",
                            "timeout_seconds": 120,
                        }
                    ]
                },
            }
        )

        result = await val.run_profile_phases(
            workspace_id="ws_db_generated_setup",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            profile=profile,
            phase_names=("setup", "pre_agent"),
        )

        assert result.all_passed
        assert [(command.phase, command.command) for command in result.commands] == [
            ("setup", "python scripts/setup.py"),
            ("db_generated_setup", "python scripts/db_generated_setup.py"),
            ("pre_agent", "python scripts/pre_agent.py"),
        ]
        assert [command.stdout_path.name for command in result.commands] == [
            "01_setup.stdout",
            "01_db_generated_setup.stdout",
            "01_pre_agent.stdout",
        ]
        assert result.commands[1].stream_ids == {
            "stdout": "validation.01_db_generated_setup.stdout",
            "stderr": "validation.01_db_generated_setup.stderr",
        }
        assert result.commands[1].metadata == {
            "database_hook": True,
            "hook_kind": "generated_setup",
            "timeout_seconds": 120,
        }
        assert [
            call.args[-1].removeprefix(
                "[ -f /workspace/.venv/bin/activate ] && . /workspace/.venv/bin/activate; "
            )
            for call in fake.calls
        ] == [
            "python scripts/setup.py",
            "python scripts/db_generated_setup.py",
            "python scripts/pre_agent.py",
        ]

    @pytest.mark.unit
    async def test_run_profile_phases_runs_db_refresh_before_healthchecks_and_validate(
        self, runner: tuple[FakeCommandRunner, ValidationRunner]
    ) -> None:
        fake, val = runner
        fake.queue_result(returncode=0, stdout="post-agent ok")
        fake.queue_result(returncode=0, stdout="refresh ok")
        fake.queue_result(returncode=0, stdout="health ok")
        fake.queue_result(returncode=0, stdout="tests ok")
        profile = WorkspaceProfile.model_validate(
            {
                "name": "db-refresh-order",
                "phases": {
                    "post_agent": ["ruff format --check"],
                    "validate": ["pytest -q"],
                },
                "database": {
                    "pre_validation_refresh": [
                        {
                            "command": "python scripts/db_refresh.py",
                            "timeout_seconds": 60,
                        }
                    ]
                },
                "validation": {
                    "healthchecks": [
                        {
                            "name": "api",
                            "command": "curl -fsS http://api:8000/healthz",
                        }
                    ]
                },
            }
        )

        result = await val.run_profile_phases(
            workspace_id="ws_db_refresh_order",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            profile=profile,
            phase_names=("post_agent", "validate"),
            run_healthchecks=True,
        )

        assert result.all_passed
        assert [(command.phase, command.command) for command in result.commands] == [
            ("post_agent", "ruff format --check"),
            ("db_refresh", "python scripts/db_refresh.py"),
            ("healthcheck", "curl -fsS http://api:8000/healthz"),
            ("validate", "pytest -q"),
        ]
        assert [command.stdout_path.name for command in result.commands] == [
            "01_post_agent.stdout",
            "01_db_refresh.stdout",
            "01_healthcheck.stdout",
            "01_validate.stdout",
        ]
        assert result.commands[1].metadata == {
            "database_hook": True,
            "hook_kind": "pre_validation_refresh",
            "timeout_seconds": 60,
        }

    @pytest.mark.unit
    async def test_db_refresh_failure_skips_healthchecks_and_validate(
        self, runner: tuple[FakeCommandRunner, ValidationRunner]
    ) -> None:
        fake, val = runner
        fake.queue_result(returncode=0, stdout="post-agent ok")
        fake.queue_result(returncode=1, stderr="refresh failed")
        fake.queue_result(returncode=0, stdout="health should not run")
        profile = WorkspaceProfile.model_validate(
            {
                "name": "db-refresh-failure",
                "phases": {
                    "post_agent": ["ruff format --check"],
                    "validate": ["pytest -q"],
                },
                "database": {"pre_validation_refresh": ["python scripts/db_refresh.py"]},
                "validation": {
                    "healthchecks": [{"name": "api", "command": "curl -fsS http://api/health"}]
                },
            }
        )

        result = await val.run_profile_phases(
            workspace_id="ws_db_refresh_fail",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            profile=profile,
            phase_names=("post_agent", "validate"),
            run_healthchecks=True,
        )

        assert not result.all_passed
        assert [(command.phase, command.reason_code) for command in result.commands] == [
            ("post_agent", "COMMAND_FAILED"),
            ("db_refresh", "DATABASE_REFRESH_FAILED"),
        ]
        assert len(fake.calls) == 2
        assert all("pytest -q" not in call.args[-1] for call in fake.calls)

    @pytest.mark.unit
    async def test_database_hook_command_failure_uses_hook_specific_reason_code(
        self, runner: tuple[FakeCommandRunner, ValidationRunner]
    ) -> None:
        fake, val = runner
        fake.queue_result(returncode=0, stdout="setup ok")
        fake.queue_result(returncode=2, stdout="partial", stderr="generated setup failed")
        fake.queue_result(returncode=0, stdout="pre-agent should not run")
        profile = WorkspaceProfile.model_validate(
            {
                "name": "db-generated-setup-failure",
                "phases": {
                    "setup": ["python scripts/setup.py"],
                    "pre_agent": ["python scripts/pre_agent.py"],
                },
                "database": {"generated_setup": ["python scripts/db_generated_setup.py"]},
            }
        )

        result = await val.run_profile_phases(
            workspace_id="ws_db_generated_setup_fail",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            profile=profile,
            phase_names=("setup", "pre_agent"),
        )

        assert not result.all_passed
        assert result.first_failure is not None
        assert result.first_failure.phase == "db_generated_setup"
        assert result.first_failure.reason_code == "DATABASE_GENERATED_SETUP_FAILED"
        assert result.first_failure.stdout_path.name == "01_db_generated_setup.stdout"
        assert result.first_failure.stderr_path.name == "01_db_generated_setup.stderr"
        assert result.first_failure.stdout_path.read_text(encoding="utf-8") == "partial"
        assert result.first_failure.stderr_path.read_text(encoding="utf-8") == (
            "generated setup failed"
        )
        assert result.first_failure.metadata == {
            "database_hook": True,
            "hook_kind": "generated_setup",
            "timeout_seconds": None,
        }
        assert len(fake.calls) == 2

    @pytest.mark.unit
    async def test_database_hook_timeout_uses_hook_specific_reason_and_logs(
        self, tmp_path: Path
    ) -> None:
        timeout_runner = _ImmediateTimeoutStreamingRunner()
        log_store = LogStore(root=tmp_path / "logs")
        val = ValidationRunner(
            runner=timeout_runner,
            artifacts_dir=tmp_path / "artifacts",
            log_store=log_store,
        )
        profile = WorkspaceProfile.model_validate(
            {
                "name": "db-refresh-timeout",
                "phases": {"validate": ["pytest -q"]},
                "database": {
                    "pre_validation_refresh": [
                        {"command": "python scripts/db_refresh.py", "timeout_seconds": 1}
                    ]
                },
            }
        )

        result = await val.run_profile_phases(
            workspace_id="ws_db_refresh_timeout",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            profile=profile,
            phase_names=("validate",),
            run_healthchecks=True,
        )

        assert not result.all_passed
        assert result.first_failure is not None
        assert result.first_failure.phase == "db_refresh"
        assert result.first_failure.reason_code == "DATABASE_REFRESH_TIMEOUT"
        assert result.first_failure.returncode == 124
        assert result.first_failure.stderr_path.read_text(encoding="utf-8") == (
            "command timed out after 1s"
        )
        assert (
            tmp_path / "logs" / "ws_db_refresh_timeout" / "validation.01_db_refresh.stderr.log"
        ).read_text(encoding="utf-8") == "command timed out after 1s"
        assert result.first_failure.metadata == {
            "database_hook": True,
            "hook_kind": "pre_validation_refresh",
            "timeout_seconds": 1,
        }
        assert any("awf-cleanup" in call for call in timeout_runner.calls)
        assert all("pytest -q" not in call[-1] for call in timeout_runner.calls)

    @pytest.mark.unit
    async def test_generated_setup_timeout_uses_hook_specific_reason(self, tmp_path: Path) -> None:
        timeout_runner = _ImmediateTimeoutStreamingRunner()
        val = ValidationRunner(
            runner=timeout_runner,
            artifacts_dir=tmp_path / "artifacts",
        )
        profile = WorkspaceProfile.model_validate(
            {
                "name": "db-generated-setup-timeout",
                "database": {
                    "generated_setup": [
                        {"command": "python scripts/db_generated_setup.py", "timeout_seconds": 1}
                    ]
                },
            }
        )

        result = await val.run_profile_phases(
            workspace_id="ws_db_generated_setup_timeout",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            profile=profile,
            phase_names=("setup",),
        )

        assert not result.all_passed
        assert result.first_failure is not None
        assert result.first_failure.phase == "db_generated_setup"
        assert result.first_failure.reason_code == "DATABASE_GENERATED_SETUP_TIMEOUT"

    @pytest.mark.unit
    async def test_db_refresh_success_then_healthcheck_failure_skips_validate(
        self, runner: tuple[FakeCommandRunner, ValidationRunner]
    ) -> None:
        fake, val = runner
        fake.queue_result(returncode=0, stdout="refresh ok")
        fake.queue_result(returncode=7, stderr="connection refused")
        profile = WorkspaceProfile.model_validate(
            {
                "name": "db-refresh-healthcheck-failure",
                "phases": {"validate": ["pytest -q"]},
                "database": {"pre_validation_refresh": ["python scripts/db_refresh.py"]},
                "validation": {
                    "healthchecks": [
                        {
                            "name": "api",
                            "command": "curl -fsS http://api:8000/healthz",
                            "timeout_seconds": 0.001,
                            "interval_seconds": 0.001,
                        }
                    ]
                },
            }
        )

        result = await val.run_profile_phases(
            workspace_id="ws_db_refresh_health_fail",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            profile=profile,
            phase_names=("validate",),
            run_healthchecks=True,
        )

        assert not result.all_passed
        assert [(command.phase, command.reason_code) for command in result.commands] == [
            ("db_refresh", "COMMAND_FAILED"),
            ("healthcheck", "HEALTHCHECK_COMMAND_FAILED"),
        ]
        assert len(fake.calls) == 2
        assert all("pytest -q" not in call.args[-1] for call in fake.calls)

    @pytest.mark.unit
    async def test_db_refresh_without_validate_commands_runs_pending_healthchecks(
        self, runner: tuple[FakeCommandRunner, ValidationRunner]
    ) -> None:
        fake, val = runner
        fake.queue_result(returncode=0, stdout="refresh ok")
        fake.queue_result(returncode=0, stdout="health ok")
        profile = WorkspaceProfile.model_validate(
            {
                "name": "db-refresh-no-validate-command",
                "database": {"pre_validation_refresh": ["python scripts/db_refresh.py"]},
                "validation": {
                    "healthchecks": [
                        {
                            "name": "api",
                            "command": "curl -fsS http://api:8000/healthz",
                        }
                    ]
                },
            }
        )

        result = await val.run_profile_phases(
            workspace_id="ws_db_refresh_no_validate",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            profile=profile,
            phase_names=("validate",),
            run_healthchecks=True,
        )

        assert result.all_passed
        assert [(command.phase, command.command) for command in result.commands] == [
            ("db_refresh", "python scripts/db_refresh.py"),
            ("healthcheck", "curl -fsS http://api:8000/healthz"),
        ]

    @pytest.mark.unit
    async def test_db_refresh_without_validate_commands_returns_pending_healthcheck_failure(
        self, runner: tuple[FakeCommandRunner, ValidationRunner]
    ) -> None:
        fake, val = runner
        fake.queue_result(returncode=0, stdout="refresh ok")
        fake.queue_result(returncode=7, stderr="connection refused")
        profile = WorkspaceProfile.model_validate(
            {
                "name": "db-refresh-no-validate-health-failure",
                "database": {"pre_validation_refresh": ["python scripts/db_refresh.py"]},
                "validation": {
                    "healthchecks": [
                        {
                            "name": "api",
                            "command": "curl -fsS http://api:8000/healthz",
                            "timeout_seconds": 0.001,
                            "interval_seconds": 0.001,
                        }
                    ]
                },
            }
        )

        result = await val.run_profile_phases(
            workspace_id="ws_db_refresh_no_validate_health_fail",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            profile=profile,
            phase_names=("validate",),
            run_healthchecks=True,
        )

        assert not result.all_passed
        assert [(command.phase, command.reason_code) for command in result.commands] == [
            ("db_refresh", "COMMAND_FAILED"),
            ("healthcheck", "HEALTHCHECK_COMMAND_FAILED"),
        ]


class TestProfileHealthChecks:
    @pytest.mark.unit
    async def test_healthcheck_timeout_attempt_retries_before_deadline(
        self, runner: tuple[FakeCommandRunner, ValidationRunner]
    ) -> None:
        fake, val = runner
        fake.queue_result(
            returncode=124,
            stderr="attempt timed out",
            reason_code="PHASE_TIMEOUT",
        )
        fake.queue_result(returncode=0, stdout="ready")
        fake.queue_result(returncode=0, stdout="tests ok")
        profile = WorkspaceProfile.model_validate(
            {
                "name": "health-timeout-retry",
                "phases": {"validate": ["pytest -q"]},
                "validation": {
                    "healthchecks": [
                        {
                            "name": "api",
                            "command": "curl -fsS http://api:8000/healthz",
                            "timeout_seconds": 1,
                            "interval_seconds": 0.001,
                            "attempt_timeout_seconds": 0.001,
                        }
                    ]
                },
            }
        )

        result = await val.run_profile_phases(
            workspace_id="ws_health_timeout_retry",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            profile=profile,
            phase_names=("validate",),
            run_healthchecks=True,
        )

        assert result.all_passed
        assert result.commands[0].phase == "healthcheck"
        assert result.commands[0].retry_count == 1

    @pytest.mark.unit
    async def test_validation_waits_until_healthcheck_succeeds(
        self, runner: tuple[FakeCommandRunner, ValidationRunner]
    ) -> None:
        fake, val = runner
        fake.queue_result(returncode=1, stderr="connection refused")
        fake.queue_result(returncode=1, stderr="still starting")
        fake.queue_result(returncode=0, stdout="ready")
        fake.queue_result(returncode=0, stdout="tests ok")
        profile = WorkspaceProfile.model_validate(
            {
                "name": "health-wait",
                "phases": {"validate": ["pytest -q"]},
                "validation": {
                    "healthchecks": [
                        {
                            "name": "api",
                            "command": "curl -fsS http://api:8000/healthz",
                            "timeout_seconds": 1,
                            "interval_seconds": 0.001,
                        }
                    ]
                },
            }
        )

        result = await val.run_profile_phases(
            workspace_id="ws_health_wait",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            profile=profile,
            phase_names=("validate",),
            run_healthchecks=True,
        )

        assert result.all_passed
        assert [(command.phase, command.reason_code) for command in result.commands] == [
            ("healthcheck", "HEALTHCHECK_OK"),
            ("validate", "COMMAND_FAILED"),
        ]
        health = result.commands[0]
        assert health.metadata["healthcheck_name"] == "api"
        assert health.metadata["healthcheck_kind"] == "command"
        assert health.metadata["attempts"] == 3
        assert health.stdout_path.name == "01_healthcheck.stdout"
        assert "attempt 1" in health.stdout_path.read_text(encoding="utf-8")
        assert "ready" in health.stdout_path.read_text(encoding="utf-8")
        assert "pytest -q" in fake.calls[-1].args[-1]
        assert all("pytest -q" not in call.args[-1] for call in fake.calls[:-1])

    @pytest.mark.unit
    async def test_healthcheck_attempt_timeout_blocks_validation_and_writes_diagnostic(
        self, tmp_path: Path
    ) -> None:
        sleeping = _SleepingRunner()
        val = ValidationRunner(runner=sleeping, artifacts_dir=tmp_path / "artifacts")
        profile = WorkspaceProfile.model_validate(
            {
                "name": "health-timeout",
                "phases": {"validate": ["pytest -q"]},
                "validation": {
                    "healthchecks": [
                        {
                            "name": "api",
                            "command": "curl -fsS http://api:8000/healthz",
                            "timeout_seconds": 0.01,
                            "interval_seconds": 0.001,
                        }
                    ]
                },
            }
        )

        result = await val.run_profile_phases(
            workspace_id="ws_health_timeout",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            profile=profile,
            phase_names=("validate",),
            run_healthchecks=True,
        )

        assert not result.all_passed
        assert result.first_failure is not None
        assert result.first_failure.phase == "healthcheck"
        assert result.first_failure.reason_code == "HEALTHCHECK_TIMEOUT"
        assert result.first_failure.metadata["attempts"] == 1
        stderr = result.first_failure.stderr_path.read_text(encoding="utf-8")
        assert "command timed out after" in stderr
        assert "health check api timed out after 0.01s" in stderr
        assert not any("pytest -q" in call[-1] for call in sleeping.calls)

    @pytest.mark.unit
    async def test_healthcheck_command_failure_preserves_latest_output_and_metadata(
        self, runner: tuple[FakeCommandRunner, ValidationRunner]
    ) -> None:
        fake, val = runner
        fake.queue_result(returncode=7, stderr="connection refused")
        profile = WorkspaceProfile.model_validate(
            {
                "name": "health-command-failure",
                "phases": {"validate": ["pytest -q"]},
                "validation": {
                    "healthchecks": [
                        {
                            "name": "api",
                            "command": "curl -fsS http://api:8000/healthz",
                            "timeout_seconds": 0.001,
                            "interval_seconds": 0.001,
                        }
                    ]
                },
            }
        )

        result = await val.run_profile_phases(
            workspace_id="ws_health_command_failure",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            profile=profile,
            phase_names=("validate",),
            run_healthchecks=True,
        )

        assert not result.all_passed
        assert len(fake.calls) == 1
        assert result.first_failure is not None
        assert result.first_failure.reason_code == "HEALTHCHECK_COMMAND_FAILED"
        assert result.first_failure.metadata["target"] == "curl -fsS http://api:8000/healthz"
        assert "connection refused" in result.first_failure.stderr_path.read_text(encoding="utf-8")
        assert "pytest -q" not in fake.calls[0].args[-1]

    @pytest.mark.unit
    async def test_healthcheck_failure_appends_diagnostic_without_reopening_command_streams(
        self, tmp_path: Path
    ) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=7, stderr="connection refused\n")
        log_store = _CountingLogStore(root=tmp_path / "logs")
        val = ValidationRunner(
            runner=fake,
            artifacts_dir=tmp_path / "artifacts",
            log_store=log_store,
        )
        profile = WorkspaceProfile.model_validate(
            {
                "name": "health-command-failure",
                "phases": {"validate": ["pytest -q"]},
                "validation": {
                    "healthchecks": [
                        {
                            "name": "api",
                            "command": "curl -fsS http://api:8000/healthz",
                            "timeout_seconds": 0.001,
                            "interval_seconds": 0.001,
                        }
                    ]
                },
            }
        )

        result = await val.run_profile_phases(
            workspace_id="ws_health_command_failure_log",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            profile=profile,
            phase_names=("validate",),
            run_healthchecks=True,
        )

        assert not result.all_passed
        assert log_store.open_command_stream_calls == ["validation.01_healthcheck"]
        stderr_log = (
            tmp_path
            / "logs"
            / "ws_health_command_failure_log"
            / "validation.01_healthcheck.stderr.log"
        ).read_text(encoding="utf-8")
        assert "connection refused" in stderr_log
        assert "health check api failed after 1 attempt(s)" in stderr_log

    @pytest.mark.unit
    async def test_http_style_healthcheck_uses_fixed_python_urllib_command(
        self, runner: tuple[FakeCommandRunner, ValidationRunner]
    ) -> None:
        fake, val = runner
        fake.queue_result(returncode=0, stdout="http status 200")
        fake.queue_result(returncode=0, stdout="tests ok")
        profile = WorkspaceProfile.model_validate(
            {
                "name": "http-health",
                "phases": {"validate": ["pytest -q"]},
                "validation": {
                    "healthchecks": [
                        {
                            "name": "api",
                            "url": "http://api:8080/healthz",
                            "expected_status": 200,
                            "timeout_seconds": 1,
                        }
                    ]
                },
            }
        )

        result = await val.run_profile_phases(
            workspace_id="ws_http_health",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            profile=profile,
            phase_names=("validate",),
            run_healthchecks=True,
        )

        assert result.all_passed
        health_args = fake.calls[0].args
        exec_index = health_args.index("awf-exec")
        cli_args = health_args[exec_index + 2 :]
        assert cli_args[:2] == ["python", "-c"]
        assert "urllib.request" in cli_args[2]
        assert cli_args[3:] == ["GET", "http://api:8080/healthz", "200", "1.0"]
        assert "http://api:8080/healthz" in result.commands[0].command
        assert result.commands[0].metadata["healthcheck_kind"] == "http"

    @pytest.mark.unit
    async def test_run_healthchecks_without_declared_checks_preserves_command_order(
        self, runner: tuple[FakeCommandRunner, ValidationRunner]
    ) -> None:
        fake, val = runner
        fake.queue_result(returncode=0, stdout="tests ok")
        profile = WorkspaceProfile.model_validate(
            {
                "name": "no-healthchecks",
                "phases": {"validate": ["pytest -q"]},
            }
        )

        result = await val.run_profile_phases(
            workspace_id="ws_no_healthchecks",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            profile=profile,
            phase_names=("validate",),
            run_healthchecks=True,
        )

        assert result.all_passed
        assert [(command.phase, command.command) for command in result.commands] == [
            ("validate", "pytest -q")
        ]
        assert result.commands[0].stdout_path.name == "01_validate.stdout"
        assert len(fake.calls) == 1

    @pytest.mark.unit
    async def test_append_healthcheck_diagnostic_ignores_non_stderr_stream_id(
        self, tmp_path: Path
    ) -> None:
        stderr = tmp_path / "health.stderr"
        stderr.write_text("connection refused\n", encoding="utf-8")
        log_store = LogStore(root=tmp_path / "logs")
        val = ValidationRunner(
            runner=FakeCommandRunner(),
            artifacts_dir=tmp_path / "artifacts",
            log_store=log_store,
        )
        result = ValidationCommandResult(
            command="curl -fsS http://api:8000/healthz",
            returncode=7,
            duration_seconds=0.01,
            stdout_path=tmp_path / "health.stdout",
            stderr_path=stderr,
            phase="healthcheck",
            stream_ids={"stderr": "validation.01_healthcheck.stdout"},
        )

        await val._append_healthcheck_stderr(
            workspace_id="ws_bad_stream_id",
            result=result,
            diagnostic="diagnostic\n",
        )

        assert stderr.read_text(encoding="utf-8").endswith("diagnostic\n")
        assert not (tmp_path / "logs" / "ws_bad_stream_id").exists()

    @pytest.mark.unit
    def test_invalid_healthcheck_configuration_helpers_fail_closed(self, tmp_path: Path) -> None:
        invalid = ProfileHealthCheck.model_construct(
            name="invalid",
            kind=None,
            command=None,
            url=None,
            method="GET",
            expected_status=200,
            timeout_seconds=1.0,
            interval_seconds=1.0,
            attempt_timeout_seconds=None,
        )
        http = ProfileHealthCheck.model_validate(
            {
                "name": "api",
                "url": "http://api:8080/healthz",
                "expected_status": 200,
            }
        )
        latest = ValidationCommandResult(
            command="health",
            returncode=1,
            duration_seconds=0.01,
            stdout_path=tmp_path / "health.stdout",
            stderr_path=tmp_path / "health.stderr",
        )

        assert _healthcheck_cli_args(invalid) == [
            "python",
            "-c",
            "import sys; print('invalid healthcheck configuration', file=sys.stderr); sys.exit(2)",
        ]
        assert _healthcheck_failure_reason(http, latest) == "HEALTHCHECK_HTTP_STATUS_MISMATCH"
        assert _healthcheck_failure_reason(invalid, latest) == ("HEALTHCHECK_INVALID_CONFIGURATION")


class TestFailureStopsEarly:
    @pytest.mark.unit
    async def test_stops_at_first_failing_command(
        self, runner: tuple[FakeCommandRunner, ValidationRunner]
    ) -> None:
        fake, val = runner
        fake.queue_result(returncode=1, stderr="lint: E501")
        # Second result should never be consumed.
        fake.queue_result(returncode=0, stdout="never runs")

        result = await val.run(
            workspace_id="ws_fail",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            test_commands=["ruff check .", "pytest -q"],
        )

        assert not result.all_passed
        assert len(result.commands) == 1
        assert result.first_failure is not None
        assert result.first_failure.command == "ruff check ."
        # FakeCommandRunner only recorded the one call.
        assert len(fake.calls) == 1
