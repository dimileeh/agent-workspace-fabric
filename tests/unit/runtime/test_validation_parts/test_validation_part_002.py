"""ValidationRunner tests with FakeCommandRunner (no docker needed)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import structlog
import yaml

from awf.common.commands import CommandResult, FakeCommandRunner
from awf.profiles import compose as profile_compose
from awf.profiles.models import ProfileHealthCheck, WorkspaceProfile
from awf.runtime import validation as validation_module
from awf.runtime import validation_identity as validation_identity_module
from awf.runtime.logs import CommandLogSinks, LogStore
from awf.runtime.validation import (
    HEALTHCHECK_HTTP_STATUS_MISMATCH,
    HEALTHCHECK_INVALID_CONFIGURATION,
    PROFILE_VALIDATION_TOOL_UNAVAILABLE,
    SETUP_DEPENDENCY_NETWORK_FAILURE,
    SETUP_DEPENDENCY_NETWORK_RETRY,
    SETUP_DEPENDENCY_NETWORK_RETRY_EXHAUSTED,
    ValidationCommandResult,
    ValidationRunner,
    _classify_setup_dependency_network_failure,
    _healthcheck_attempt_timeout,
    _healthcheck_cli_args,
    _healthcheck_failure_reason,
    profile_validation_tool_preflight_findings,
)
from awf.runtime.validation_identity import (
    environment_identity_digest,
    environment_identity_inputs,
    resolved_profile_digest,
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


@pytest.mark.unit
async def test_setup_dependency_network_exhaustion_reports_setup_retry_budget_only(
    tmp_path: Path,
) -> None:
    fake = FakeCommandRunner()
    val = ValidationRunner(
        runner=fake,
        artifacts_dir=tmp_path / "artifacts",
        setup_retry_budget=1,
        setup_retry_backoff_seconds=(0,),
    )
    profile = WorkspaceProfile.model_validate(
        {
            "name": "setup-retry",
            "phases": {"setup": ["uv sync --extra dev"]},
            "validation": {"retry_budget": 3},
        }
    )
    fake.queue_result(returncode=1, stderr=_uv_pypi_dns_failure())
    fake.queue_result(returncode=1, stderr=_uv_pypi_dns_failure())

    result = await val.run_profile_phases(
        workspace_id="ws_setup_retry",
        compose_project=_COMPOSE_PROJECT,
        compose_file=_COMPOSE_FILE,
        profile=profile,
        phase_names=("setup",),
    )

    assert not result.all_passed
    assert len(fake.calls) == 2
    command = result.commands[0]
    assert command.reason_code == SETUP_DEPENDENCY_NETWORK_FAILURE
    assert command.retry_count == 1
    retry_metadata = command.metadata["setup_dependency_network"]
    assert retry_metadata["retry_count"] == 1
    assert retry_metadata["retry_budget"] == 1
    assert retry_metadata["retry_exhausted"] is True


@pytest.mark.unit
async def test_setup_dependency_network_failure_exhaustion_has_precise_reason(
    tmp_path: Path,
) -> None:
    fake = FakeCommandRunner()
    val = ValidationRunner(
        runner=fake,
        artifacts_dir=tmp_path / "artifacts",
        setup_retry_budget=2,
        setup_retry_backoff_seconds=(0,),
    )
    profile = WorkspaceProfile.model_validate(
        {
            "name": "setup-retry",
            "phases": {"setup": ["uv sync --extra dev"]},
        }
    )
    for _ in range(3):
        fake.queue_result(returncode=1, stderr=_uv_pypi_dns_failure())

    result = await val.run_profile_phases(
        workspace_id="ws_setup_retry",
        compose_project=_COMPOSE_PROJECT,
        compose_file=_COMPOSE_FILE,
        profile=profile,
        phase_names=("setup",),
    )

    assert not result.all_passed
    assert len(fake.calls) == 3
    command = result.commands[0]
    assert command.reason_code == SETUP_DEPENDENCY_NETWORK_FAILURE
    assert command.retry_count == 2
    retry_metadata = command.metadata["setup_dependency_network"]
    assert retry_metadata["retry_count"] == 2
    assert retry_metadata["retry_budget"] == 2
    assert retry_metadata["retry_exhausted"] is True
    assert len(str(retry_metadata["diagnostic"])) <= 1000 + len("...[truncated]")


@pytest.mark.unit
async def test_deterministic_setup_failure_is_not_retried(tmp_path: Path) -> None:
    fake = FakeCommandRunner()
    val = ValidationRunner(
        runner=fake,
        artifacts_dir=tmp_path / "artifacts",
        setup_retry_budget=2,
        setup_retry_backoff_seconds=(0,),
    )
    profile = WorkspaceProfile.model_validate(
        {
            "name": "setup-retry",
            "phases": {"setup": ["uv sync --extra dev"]},
        }
    )
    fake.queue_result(
        returncode=1,
        stderr=(
            "error: Failed to resolve dependencies\n"
            "  Caused by: No solution found when resolving dependencies\n"
            "  lockfile is out of date"
        ),
    )

    result = await val.run_profile_phases(
        workspace_id="ws_setup_retry",
        compose_project=_COMPOSE_PROJECT,
        compose_file=_COMPOSE_FILE,
        profile=profile,
        phase_names=("setup",),
    )

    assert not result.all_passed
    assert len(fake.calls) == 1
    assert result.commands[0].reason_code == "COMMAND_FAILED"
    assert "setup_dependency_network" not in result.commands[0].metadata


@pytest.mark.unit
async def test_setup_dependency_retry_logs_redact_and_truncate_command_credentials(
    tmp_path: Path,
) -> None:
    fake = FakeCommandRunner()
    val = ValidationRunner(
        runner=fake,
        artifacts_dir=tmp_path / "artifacts",
        setup_retry_budget=1,
        setup_retry_backoff_seconds=(0,),
    )
    raw_secret = "ghp_1234567890abcdef"
    long_secret_command = (
        "uv sync --extra dev "
        f"--index-url https://user:{raw_secret}@files.pythonhosted.org/simple "
        + ("--config-setting xxxxxxxxxx " * 80)
    )
    profile = WorkspaceProfile.model_validate(
        {
            "name": "setup-retry",
            "phases": {"setup": [long_secret_command]},
        }
    )
    fake.queue_result(returncode=1, stderr=_uv_pypi_dns_failure())
    fake.queue_result(returncode=1, stderr=_uv_pypi_dns_failure())

    with structlog.testing.capture_logs() as captured:
        result = await val.run_profile_phases(
            workspace_id="ws_setup_retry_secret_command",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            profile=profile,
            phase_names=("setup",),
        )

    assert not result.all_passed
    retry_log = next(
        event for event in captured if event["event"] == "validation.setup_dependency_network_retry"
    )
    exhausted_log = next(
        event
        for event in captured
        if event["event"] == "validation.setup_dependency_network_retry_exhausted"
    )
    assert retry_log["reason_code"] == SETUP_DEPENDENCY_NETWORK_RETRY
    assert exhausted_log["reason_code"] == SETUP_DEPENDENCY_NETWORK_RETRY_EXHAUSTED
    for log_entry in (retry_log, exhausted_log):
        command = str(log_entry["command"])
        assert raw_secret not in command
        assert f"user:{raw_secret}" not in command
        assert "https://[redacted]@files.pythonhosted.org/simple" in command
        assert command.endswith("...[truncated]")
        assert len(command) <= 500 + len("...[truncated]")


@pytest.mark.unit
async def test_setup_dependency_retry_preserves_metadata_when_later_failure_reclassifies(
    tmp_path: Path,
) -> None:
    fake = FakeCommandRunner()
    val = ValidationRunner(
        runner=fake,
        artifacts_dir=tmp_path / "artifacts",
        setup_retry_budget=2,
        setup_retry_backoff_seconds=(0,),
    )
    profile = WorkspaceProfile.model_validate(
        {
            "name": "setup-retry",
            "phases": {"setup": ["uv sync --extra dev"]},
        }
    )
    fake.queue_result(returncode=1, stderr=_uv_pypi_dns_failure())
    fake.queue_result(returncode=1, stderr="local post-install hook failed")

    result = await val.run_profile_phases(
        workspace_id="ws_setup_retry",
        compose_project=_COMPOSE_PROJECT,
        compose_file=_COMPOSE_FILE,
        profile=profile,
        phase_names=("setup",),
    )

    assert not result.all_passed
    assert len(fake.calls) == 2
    assert result.commands[0].reason_code == "COMMAND_FAILED"
    assert result.commands[0].retry_count == 1
    retry_metadata = result.commands[0].metadata["setup_dependency_network"]
    assert retry_metadata["retry_count"] == 1
    assert retry_metadata["retry_budget"] == 2
    assert retry_metadata["retry_exhausted"] is False
    assert retry_metadata["recovered"] is False
    assert retry_metadata["package"] == "docker==7.1.0"
    assert retry_metadata["host"] == "files.pythonhosted.org"
    assert retry_metadata["attempts"][0]["attempt"] == 1
    assert retry_metadata["attempts"][0]["retry_number"] == 1


@pytest.mark.unit
def test_setup_dependency_network_diagnostic_normalization_uses_bounded_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    normalized_lengths: list[int] = []
    original_sub = validation_module.re.sub

    def record_normalized_input(
        pattern: str,
        repl: str,
        string: str,
        count: int = 0,
        flags: int = 0,
    ) -> str:
        normalized_lengths.append(len(string))
        return original_sub(pattern, repl, string, count=count, flags=flags)

    monkeypatch.setattr(validation_module.re, "sub", record_normalized_input)

    classification = _classify_setup_dependency_network_failure(
        command="uv sync --extra dev",
        returncode=1,
        stdout="progress line\n" * 1000,
        stderr=_uv_pypi_dns_failure(),
    )

    assert classification is not None
    assert normalized_lengths
    assert max(normalized_lengths) <= (
        4 * validation_module._SETUP_DEPENDENCY_NETWORK_DIAGNOSTIC_LIMIT
    )


@pytest.mark.unit
def test_setup_dependency_diagnostics_redact_and_truncate_secret_output() -> None:
    raw_secret = "ghp_1234567890abcdef"
    classification = _classify_setup_dependency_network_failure(
        command="uv sync --extra dev",
        returncode=1,
        stdout="",
        stderr=(
            _uv_pypi_dns_failure()
            + "\nhttps://user:pass@files.pythonhosted.org/simple/docker/"
            + f"\nAuthorization: Bearer {raw_secret}"
            + "\nPIP_INDEX_URL=https://token:secret@files.pythonhosted.org/simple"
            + "\n"
            + ("DNS timeout while downloading docker==7.1.0 " * 200)
        ),
    )

    assert classification is not None
    diagnostic = str(classification.metadata["diagnostic"])
    assert raw_secret not in diagnostic
    assert "user:pass" not in diagnostic
    assert "token:secret" not in diagnostic
    assert "[redacted]" in diagnostic
    assert diagnostic.endswith("...[truncated]")
    assert len(diagnostic) <= 1000 + len("...[truncated]")


@pytest.mark.unit
def test_environment_identity_digest_is_stable_across_mapping_and_service_order() -> None:
    first = _identity_profile()
    second = _identity_profile(
        runtime={
            "toolchain_image": "ghcr.io/acme/toolchain:1",
            "environment": {
                "API_TOKEN": "sk-secret-value",
                "PYTHON_VERSION": "3.12",
            },
            "agent_image": "ghcr.io/acme/agent:1",
        },
        services=[
            {
                "name": "postgres",
                "environment": {"POSTGRES_PASSWORD": "postgres-secret"},
                "image": "postgres:16",
            },
            {
                "ports": [(8000, 8000)],
                "healthcheck_cmd": "curl -fsS http://api:8000/health",
                "depends_on": ["postgres"],
                "environment": {"LOG_LEVEL": "debug", "DATABASE_URL": "postgres://secret"},
                "image": "ghcr.io/acme/api:1",
                "name": "api",
            },
        ],
    )

    assert environment_identity_digest(first) == environment_identity_digest(second)
    assert environment_identity_inputs(first) == environment_identity_inputs(second)


@pytest.mark.unit
def test_environment_identity_digest_changes_for_dind_daemon_image() -> None:
    # The DinD daemon image is rendered into the per-workspace compose stack, so
    # two runs that differ only by docker.dind_image must not collide on the
    # environment identity digest (else validation provenance treats different
    # Docker daemons as identical).
    baseline = _identity_profile()
    overridden = _identity_profile(
        docker={
            "mode": "dind",
            "compose_files": ["compose.yml", "compose.override.yml"],
            "project_directory": ".",
            "startup_timeout_seconds": 120,
            "dind_image": "ghcr.io/example/dind:buildx",
        }
    )

    assert environment_identity_inputs(baseline)["docker"]["dind_image"] == "docker:27-dind"
    assert (
        environment_identity_inputs(overridden)["docker"]["dind_image"]
        == "ghcr.io/example/dind:buildx"
    )
    assert environment_identity_digest(baseline) != environment_identity_digest(overridden)


def test_environment_identity_digest_changes_for_parallel_coverage_policy() -> None:
    serial = _identity_profile()
    parallel = _identity_profile(
        validation={
            "coverage": {
                "minimum_percent": 99,
                "command": "pytest --cov=awf",
                "parallel_workers": 3,
            }
        }
    )

    assert environment_identity_digest(serial) != environment_identity_digest(parallel)


@pytest.mark.unit
def test_environment_identity_sorts_services_with_nullable_dockerfile() -> None:
    profile = _identity_profile()
    service_without_dockerfile = profile.services[0].model_copy(update={"dockerfile": None})
    service_with_dockerfile = profile.services[0].model_copy(
        update={"dockerfile": "services/api.Dockerfile"}
    )
    mixed_profile = profile.model_copy(
        update={"services": [service_with_dockerfile, service_without_dockerfile]}
    )

    inputs = environment_identity_inputs(mixed_profile)

    assert [service["dockerfile"] for service in inputs["services"]] == [
        None,
        "services/api.Dockerfile",
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    "override",
    [
        {"runtime": {"agent_image": "ghcr.io/acme/agent:2"}},
        {"runtime": {"toolchain_image": "ghcr.io/acme/toolchain:2"}},
        {"docker": {"mode": "none"}},
        {
            "services": [
                {"name": "api", "image": "ghcr.io/acme/api:2"},
                {"name": "postgres", "image": "postgres:16"},
            ]
        },
        {
            "validation": {
                "healthchecks": [
                    {
                        "name": "api",
                        "command": "curl -fsS http://api:8000/ready",
                    }
                ]
            }
        },
    ],
)
def test_environment_identity_digest_changes_for_runtime_toolchain_inputs(
    override: dict[str, object],
) -> None:
    assert environment_identity_digest(_identity_profile()) != environment_identity_digest(
        _identity_profile(**override)
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "healthcheck",
    [
        {
            "name": "api",
            "command": "curl -fsS http://api:8000/health",
            "timeout_seconds": 30,
        },
        {
            "name": "api",
            "command": "curl -fsS http://api:8000/health",
            "timeout_seconds": 20,
            "interval_seconds": 0.5,
        },
        {
            "name": "api",
            "url": "http://api:8000/health",
            "expected_status": 204,
            "interval_seconds": 0.5,
            "attempt_timeout_seconds": 2,
        },
    ],
)
def test_environment_identity_digest_changes_for_healthcheck_wait_policy(
    healthcheck: dict[str, object],
) -> None:
    assert environment_identity_digest(_identity_profile()) != environment_identity_digest(
        _identity_profile(validation={"healthchecks": [healthcheck]})
    )


@pytest.mark.unit
def test_healthcheck_helpers_handle_invalid_constructed_configuration(tmp_path: Path) -> None:
    healthcheck = ProfileHealthCheck.model_construct(name="invalid", command=None, url=None)
    latest = ValidationCommandResult(
        command="healthcheck",
        returncode=1,
        duration_seconds=0.1,
        stdout_path=tmp_path / "health.stdout",
        stderr_path=tmp_path / "health.stderr",
    )

    assert _healthcheck_cli_args(healthcheck)[0:2] == ["python", "-c"]
    assert _healthcheck_attempt_timeout(healthcheck, remaining_seconds=0) == 0.001
    assert _healthcheck_failure_reason(healthcheck, latest) == HEALTHCHECK_INVALID_CONFIGURATION


@pytest.mark.unit
def test_http_healthcheck_failure_reason_reports_status_mismatch(tmp_path: Path) -> None:
    healthcheck = ProfileHealthCheck(
        name="api",
        url="https://api.example.test/healthz",
        expected_status=204,
    )
    latest = ValidationCommandResult(
        command="healthcheck",
        returncode=1,
        duration_seconds=0.1,
        stdout_path=tmp_path / "health.stdout",
        stderr_path=tmp_path / "health.stderr",
    )

    assert _healthcheck_failure_reason(healthcheck, latest) == HEALTHCHECK_HTTP_STATUS_MISMATCH


@pytest.mark.unit
async def test_append_healthcheck_stderr_skips_invalid_stream_id(tmp_path: Path) -> None:
    log_store = _CountingLogStore(root=tmp_path / "logs")
    validation = ValidationRunner(
        runner=FakeCommandRunner(),
        artifacts_dir=tmp_path / "artifacts",
        log_store=log_store,
    )
    stderr_path = tmp_path / "health.stderr"
    stderr_path.write_text("initial\n", encoding="utf-8")
    result = ValidationCommandResult(
        command="healthcheck",
        returncode=1,
        duration_seconds=0.1,
        stdout_path=tmp_path / "health.stdout",
        stderr_path=stderr_path,
        stream_ids={"stderr": "validation.01_healthcheck.stdout"},
    )

    await validation._append_healthcheck_stderr(
        workspace_id="ws_health",
        result=result,
        diagnostic="diagnostic\n",
    )

    assert stderr_path.read_text(encoding="utf-8") == "initial\ndiagnostic\n"
    assert not (tmp_path / "logs" / "ws_health").exists()


@pytest.mark.unit
async def test_run_profile_phases_can_skip_coverage_for_targeted_edit_gate(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout="ok\n")
    validation = ValidationRunner(runner=runner, artifacts_dir=tmp_path / "artifacts")
    profile = WorkspaceProfile.model_validate(
        {
            "name": "targeted",
            "validation": {
                "coverage": {
                    "minimum_percent": 99,
                    "command": "pytest --cov=awf",
                }
            },
            "phases": {"validate": ["python -m pytest tests/unit/test_fast.py -q"]},
        }
    )

    result = await validation.run_profile_phases(
        workspace_id="ws_targeted",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        profile=profile,
        phase_names=("validate",),
        include_coverage=False,
    )

    assert result.all_passed
    assert result.coverage is None
    assert [call.args[-1] for call in runner.calls] == [
        "[ -f /workspace/.venv/bin/activate ] && . /workspace/.venv/bin/activate; "
        "python -m pytest tests/unit/test_fast.py -q"
    ]


@pytest.mark.unit
def test_profile_tool_preflight_flags_uv_run_dev_tool_without_dev_extra() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "bad-dev-tool-profile",
            "phases": {
                "setup": ["uv sync --extra dev"],
                "validate": [
                    "uv run ruff check src tests",
                    "uv run --python 3.12 --extra dev pytest tests/unit -q",
                ],
            },
        }
    )

    findings = profile_validation_tool_preflight_findings(profile)

    assert len(findings) == 1
    assert findings[0].command == "uv run ruff check src tests"
    assert findings[0].tool == "ruff"
    assert findings[0].reason_code == PROFILE_VALIDATION_TOOL_UNAVAILABLE


@pytest.mark.unit
async def test_profile_tool_preflight_blocks_before_agent_for_missing_dev_scope(
    tmp_path: Path,
) -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "bad-dev-tool-profile",
            "phases": {
                "setup": ["uv sync --extra dev"],
                "validate": ["uv run mypy src/awf"],
            },
        }
    )
    runner = FakeCommandRunner()
    validation = ValidationRunner(runner=runner, artifacts_dir=tmp_path / "artifacts")

    result = await validation.run_profile_tool_preflight(
        workspace_id="ws_bad_profile",
        profile=profile,
    )

    assert not result.all_passed
    assert result.first_failure is not None
    assert result.first_failure.phase == "profile_preflight"
    assert result.first_failure.reason_code == PROFILE_VALIDATION_TOOL_UNAVAILABLE
    assert "uv run mypy src/awf" in result.first_failure.stderr_path.read_text(encoding="utf-8")
    assert runner.calls == []


@pytest.mark.unit
async def test_profile_tool_preflight_writes_log_stream_for_missing_dev_scope(
    tmp_path: Path,
) -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "bad-dev-tool-profile",
            "phases": {
                "setup": ["uv sync --extra dev"],
                "validate": ["uv run pytest tests/unit -q"],
            },
        }
    )
    log_store = _CountingLogStore(root=tmp_path / "logs")
    validation = ValidationRunner(
        runner=FakeCommandRunner(),
        artifacts_dir=tmp_path / "artifacts",
        log_store=log_store,
    )

    result = await validation.run_profile_tool_preflight(
        workspace_id="ws_bad_profile",
        profile=profile,
    )

    assert not result.all_passed
    assert log_store.open_command_stream_calls == ["validation.01_profile_preflight"]
    logged_stderr = (
        tmp_path / "logs" / "ws_bad_profile" / "validation.01_profile_preflight.stderr.log"
    ).read_text(encoding="utf-8")
    assert "uv run pytest tests/unit -q" in logged_stderr


@pytest.mark.unit
def test_awf_self_profile_validation_commands_preserve_dev_dependency_scope() -> None:
    profile_path = Path(__file__).resolve().parents[4] / ".awf" / "workspace.yml"
    profile = WorkspaceProfile.model_validate(
        yaml.safe_load(profile_path.read_text(encoding="utf-8"))["awf"]
    )

    assert profile_validation_tool_preflight_findings(profile) == []


@pytest.mark.unit
@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("not a valid ' shell", None),
        ("uv", None),
        ("uv sync", None),
        ("uv run", None),
        ("uv run --python 3.12 -- ruff check src", {"tool": "ruff", "has_dev_scope": False}),
        ("uv run --python 3.12 --dev ruff check src", {"tool": "ruff", "has_dev_scope": True}),
        (
            "uv run --python 3.12 --extra=dev ruff check src",
            {"tool": "ruff", "has_dev_scope": True},
        ),
        ("uv run --python 3.12 --group=dev mypy src/awf", {"tool": "mypy", "has_dev_scope": True}),
        (
            "uv run --python 3.12 --extra docs ruff check src",
            {"tool": "ruff", "has_dev_scope": False},
        ),
        ("uv run --python 3.12 --extra", None),
    ],
)
def test_uv_run_metadata_handles_dev_scope_variants(
    command: str,
    expected: dict[str, object] | None,
) -> None:
    assert validation_module._uv_run_metadata(command) == expected  # noqa: SLF001


@pytest.mark.unit
@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("bad ' shell", False),
        ("python -m pytest", False),
        ("uv run --python 3.12 ruff check src", False),
        ("uv run --python 3.12 --group dev pytest tests/unit -q", True),
        ("uv sync --extra=dev", True),
        ("uv sync --group docs", False),
        ("uv sync --all-extras", True),
    ],
)
def test_uv_command_has_dev_scope_for_sync_and_run_variants(
    command: str,
    expected: bool,
) -> None:
    assert validation_module._uv_command_has_dev_scope(command) is expected  # noqa: SLF001


@pytest.mark.unit
def test_profile_tool_preflight_ignores_non_dev_tools_and_scoped_dev_tools() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "mixed-dev-tool-profile",
            "phases": {
                "setup": ["uv sync --group dev"],
                "validate": [
                    "python -m pytest tests/unit -q",
                    "uv run --python 3.12 --group dev ruff check src tests",
                    "uv run --python 3.12 node scripts/check-docs.js",
                    "uv run --python 3.12 pytest tests/unit -q",
                ],
            },
        }
    )

    findings = profile_validation_tool_preflight_findings(profile)

    assert [finding.tool for finding in findings] == ["pytest"]
    assert findings[0].command == "uv run --python 3.12 pytest tests/unit -q"


@pytest.mark.unit
async def test_run_profile_coverage_uses_full_gate_throttle(tmp_path: Path) -> None:
    class BlockingRunner(FakeCommandRunner):
        async def run(
            self,
            args: list[str],
            *,
            input_bytes: bytes | None = None,
            cwd: str | None = None,
        ) -> CommandResult:
            del input_bytes, cwd
            nonlocal concurrent, max_concurrent
            self.calls.append(type("RecordedCall", (), {"args": args})())
            concurrent += 1
            max_concurrent = max(max_concurrent, concurrent)
            first_started.set()
            if not release_first.is_set():
                await release_first.wait()
            concurrent -= 1
            return CommandResult(returncode=0, stdout="TOTAL 1 0 100%\n", stderr="")

    runner = BlockingRunner()
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    concurrent = 0
    max_concurrent = 0
    validation = ValidationRunner(runner=runner, artifacts_dir=tmp_path / "artifacts")
    profile = WorkspaceProfile.model_validate(
        {
            "name": "throttled",
            "validation": {
                "strategy": {"full_gate_concurrency": 1},
                "coverage": {
                    "minimum_percent": 99,
                    "command": "pytest --cov=awf",
                },
            },
        }
    )

    first = asyncio.create_task(
        validation.run_profile_coverage(
            workspace_id="ws_throttle_1",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            profile=profile,
            phase="final_coverage",
        )
    )
    await first_started.wait()
    second = asyncio.create_task(
        validation.run_profile_coverage(
            workspace_id="ws_throttle_2",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            profile=profile,
            phase="final_coverage",
        )
    )
    await asyncio.sleep(0)
    assert max_concurrent == 1

    release_first.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert first_result is not None and first_result.ok
    assert second_result is not None and second_result.ok
    assert max_concurrent == 1


@pytest.mark.unit
def test_environment_identity_digest_changes_for_database_hooks() -> None:
    refresh_hook = {
        "database": {
            "pre_validation_refresh": [
                {"command": "python scripts/db_refresh.py", "timeout_seconds": 120}
            ]
        }
    }
    changed_refresh_timeout = {
        "database": {
            "pre_validation_refresh": [
                {"command": "python scripts/db_refresh.py", "timeout_seconds": 121}
            ]
        }
    }

    assert environment_identity_digest(_identity_profile()) != environment_identity_digest(
        _identity_profile(**refresh_hook)
    )
    assert environment_identity_digest(_identity_profile(**refresh_hook)) != (
        environment_identity_digest(_identity_profile(**changed_refresh_timeout))
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "alembic_policy",
    [
        {"enabled": True},
        {"enabled": True, "config_path": "db/alembic.ini"},
        {"enabled": True, "script_location": "db/migrations"},
        {"enabled": True, "fail_on_unconfigured": False},
    ],
)
def test_environment_identity_digest_changes_for_alembic_validation_policy(
    alembic_policy: dict[str, object],
) -> None:
    profile = _identity_profile(validation={"alembic": alembic_policy})

    assert environment_identity_digest(_identity_profile()) != environment_identity_digest(profile)
    assert environment_identity_inputs(profile)["validation"]["alembic"] == {
        "enabled": alembic_policy.get("enabled", False),
        "config_path": alembic_policy.get("config_path", "alembic.ini"),
        "script_location": alembic_policy.get("script_location"),
        "fail_on_unconfigured": alembic_policy.get("fail_on_unconfigured", True),
    }


@pytest.mark.unit
def test_environment_identity_digest_excludes_non_validation_profile_metadata() -> None:
    baseline = _identity_profile()
    changed_metadata = baseline.model_copy(
        deep=True,
        update={
            "description": "A different human-facing description.",
            "monitor": baseline.monitor.model_copy(
                update={"initial_review_grace_period_seconds": 3600.0}
            ),
            "planning": baseline.planning.model_copy(
                update={
                    "plan_path": "docs/alternate/{workspace_id}.md",
                    "conformance_report_path": "docs/alternate/{workspace_id}.json",
                }
            ),
        },
    )

    assert environment_identity_digest(baseline) == environment_identity_digest(changed_metadata)


@pytest.mark.unit
def test_resolved_profile_digest_is_canonical_and_covers_full_profile() -> None:
    first = _identity_profile()
    reordered = _identity_profile(
        runtime={
            "environment": {
                "API_TOKEN": "sk-secret-value",
                "PYTHON_VERSION": "3.12",
            },
            "toolchain_image": "ghcr.io/acme/toolchain:1",
            "agent_image": "ghcr.io/acme/agent:1",
        }
    )
    changed_description = first.model_copy(update={"description": "different"})

    assert resolved_profile_digest(first) == resolved_profile_digest(reordered)
    assert resolved_profile_digest(first) != resolved_profile_digest(changed_description)


@pytest.mark.unit
def test_environment_identity_inputs_sanitize_environment_and_secret_values() -> None:
    inputs = environment_identity_inputs(
        _identity_profile(ports={"database": "postgres://user:password@127.0.0.1:5432/app"})
    )
    rendered = str(inputs)

    assert "sk-secret-value" not in rendered
    assert "postgres://secret" not in rendered
    assert "postgres-secret" not in rendered
    assert "secret/data/github/token" not in rendered
    assert "postgres://user:password@127.0.0.1:5432/app" not in rendered
    assert inputs["ports"] == [
        {
            "name": "database",
            "value_sha256": (
                "sha256:30e7d9f55ac1fd625ddc82a1f36e9e1e827f18f3f90a4f32630ca8b4f1a7bc6e"
            ),
        }
    ]
    assert inputs["security"]["network_posture"] == "restricted"
    assert "allowlist" not in inputs["security"]
    runtime_env = inputs["runtime"]["environment"]
    assert runtime_env == [
        {
            "name": "API_TOKEN",
            "value_sha256": (
                "sha256:6a34e9cf66e854e6e1b79ceebaac12897fd6845a57d2cf367ca33a74fdbc1afb"
            ),
        },
        {
            "name": "PYTHON_VERSION",
            "value_sha256": (
                "sha256:33307da56af13f791584518fef2e49641180bfbf2ef7b2d256c9ab6fad564f80"
            ),
        },
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    "endpoint_override",
    [
        {"path": "/ready"},
        {"service": "postgres", "port": 5432},
        {"port": 8001},
        {"health": {"path": "/ready", "expected_status": 204}},
        {"visibility": "validation"},
    ],
)
def test_environment_identity_digest_changes_for_app_endpoint_inputs(
    endpoint_override: dict[str, object],
) -> None:
    assert environment_identity_digest(_identity_profile_with_endpoint()) != (
        environment_identity_digest(_identity_profile_with_endpoint(**endpoint_override))
    )


@pytest.mark.unit
def test_environment_identity_inputs_include_app_endpoints_and_generated_endpoint_env() -> None:
    inputs = environment_identity_inputs(_identity_profile_with_endpoint())

    assert inputs["app_endpoints"] == [
        {
            "name": "api",
            "service": "api",
            "scheme": "http",
            "port": 8000,
            "path": "/",
            "internal_url": "http://api:8000/",
            "visibility": "agent",
            "health": {
                "path": "/health",
                "method": "GET",
                "expected_status": 200,
                "internal_url": "http://api:8000/health",
            },
        }
    ]
    assert [entry["name"] for entry in inputs["generated_endpoint_environment"]] == [
        "AWF_APP_ENDPOINTS_JSON",
        "AWF_APP_ENDPOINT_API_URL",
    ]
    assert all(
        entry["value_sha256"].startswith("sha256:")
        for entry in inputs["generated_endpoint_environment"]
    )


@pytest.mark.unit
def test_environment_identity_reuses_resolved_app_endpoints_for_generated_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    original_resolve_app_endpoints = profile_compose.resolve_app_endpoints

    def counted_resolve_app_endpoints(
        profile: WorkspaceProfile,
        *,
        include_internal: bool = True,
    ) -> tuple[dict[str, Any], ...]:
        nonlocal calls
        calls += 1
        return original_resolve_app_endpoints(profile, include_internal=include_internal)

    monkeypatch.setattr(
        profile_compose,
        "resolve_app_endpoints",
        counted_resolve_app_endpoints,
    )
    monkeypatch.setattr(
        validation_identity_module,
        "resolve_app_endpoints",
        counted_resolve_app_endpoints,
    )

    inputs = environment_identity_inputs(_identity_profile_with_endpoint())

    assert calls == 1
    assert inputs["app_endpoints"][0]["internal_url"] == "http://api:8000/"
