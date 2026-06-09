"""ValidationRunner tests with FakeCommandRunner (no docker needed)."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from awf.common.commands import CommandResult, FakeCommandRunner
from awf.profiles.models import WorkspaceProfile
from awf.runtime import validation as validation_module
from awf.runtime.logs import CommandLogSinks, LogStore
from awf.runtime.validation import (
    SETUP_DEPENDENCY_NETWORK_FAILURE,
    ValidationCommandResult,
    ValidationRunner,
    _classify_setup_dependency_network_failure,
    _classify_setup_dependency_network_result,
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
def test_setup_dependency_network_classifier_extracts_uv_pypi_dns_failure() -> None:
    classification = _classify_setup_dependency_network_failure(
        command="uv sync --extra dev",
        returncode=1,
        stdout="",
        stderr=_uv_pypi_dns_failure(),
    )

    assert classification is not None
    assert classification.retryable is True
    assert classification.reason_code == SETUP_DEPENDENCY_NETWORK_FAILURE
    assert classification.transient_category == "dns"
    assert classification.package == "docker==7.1.0"
    assert classification.host == "files.pythonhosted.org"
    assert classification.metadata["retryable"] is True
    assert classification.metadata["diagnostic"]
    assert len(str(classification.metadata["diagnostic"])) <= 1000 + len("...[truncated]")


@pytest.mark.unit
def test_setup_dependency_network_result_reads_missing_captured_stream_from_artifact(
    tmp_path: Path,
) -> None:
    stderr_path = tmp_path / "setup.stderr"
    stderr_path.write_text(_uv_pypi_dns_failure(), encoding="utf-8")
    result = ValidationCommandResult(
        command="uv sync --extra dev",
        returncode=1,
        duration_seconds=0.1,
        stdout_path=tmp_path / "setup.stdout",
        stderr_path=stderr_path,
        phase="setup",
        captured_stdout="",
        captured_stderr=None,
    )

    classification = _classify_setup_dependency_network_result(result)

    assert classification is not None
    assert classification.reason_code == SETUP_DEPENDENCY_NETWORK_FAILURE
    assert classification.transient_category == "dns"
    assert classification.package == "docker==7.1.0"
    assert classification.host == "files.pythonhosted.org"


@pytest.mark.unit
def test_setup_dependency_network_classifier_skips_index_url_credentials_for_package() -> None:
    raw_secret = "ghp_1234567890abcdef"
    classification = _classify_setup_dependency_network_failure(
        command=(f"uv sync --index-url https://user:{raw_secret}@files.pythonhosted.org/simple"),
        returncode=1,
        stdout="",
        stderr=_uv_pypi_dns_failure(package="docker==7.1.0"),
    )

    assert classification is not None
    assert classification.package == "docker==7.1.0"
    assert raw_secret not in str(classification.metadata)
    assert "user:ghp_" not in str(classification.metadata)
    assert classification.host == "files.pythonhosted.org"


@pytest.mark.unit
def test_setup_dependency_network_classifier_does_not_extract_jwt_secret_as_host() -> None:
    raw_secret = ".".join(
        [
            "eyJhbGciOiJIUzI1NiJ9",
            "eyJzdWIiOiIxMjM0NTY3ODkwIn0",
            "signature123",
        ]
    )
    classification = _classify_setup_dependency_network_failure(
        command=f"TOKEN={raw_secret} uv sync --extra dev",
        returncode=1,
        stdout="",
        stderr=(
            "Failed to download docker==7.1.0 after request retries: "
            "temporary failure in name resolution"
        ),
    )

    assert classification is not None
    assert classification.host is None
    assert raw_secret not in str(classification.metadata)


@pytest.mark.unit
def test_setup_dependency_network_classifier_ignores_version_like_fallback_host() -> None:
    classification = _classify_setup_dependency_network_failure(
        command="pip install docker==7.1.0",
        returncode=1,
        stdout="",
        stderr=(
            "Failed to download docker==7.1.0 after request retries: "
            "dns error: failed to lookup address information"
        ),
    )

    assert classification is not None
    assert classification.reason_code == SETUP_DEPENDENCY_NETWORK_FAILURE
    assert classification.package == "docker==7.1.0"
    assert classification.host is None


@pytest.mark.unit
def test_setup_dependency_package_extraction_reads_archive_name_version() -> None:
    assert (  # noqa: SLF001
        validation_module._extract_setup_dependency_package("Using cached docker-7.1.0.tar.gz")
        == "docker==7.1.0"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "artifact_name",
    [
        "docker-7.1.0.tar.gz",
        "package.whl",
        "setup.cfg",
        "pyproject.yml",
        "pyproject.yaml",
        "metadata.json",
    ],
)
def test_setup_dependency_network_classifier_ignores_artifact_like_fallback_hosts(
    artifact_name: str,
) -> None:
    classification = _classify_setup_dependency_network_failure(
        command="pip install docker==7.1.0",
        returncode=1,
        stdout="",
        stderr=(
            f"Failed to download {artifact_name} after request retries: "
            "dns error: failed to lookup address information"
        ),
    )

    assert classification is not None
    assert classification.reason_code == SETUP_DEPENDENCY_NETWORK_FAILURE
    assert classification.package == "docker==7.1.0"
    assert classification.host is None


@pytest.mark.unit
def test_setup_dependency_network_classifier_skips_uv_run_script_dns_failure() -> None:
    classification = _classify_setup_dependency_network_failure(
        command="uv run python scripts/bootstrap.py",
        returncode=1,
        stdout="",
        stderr=(
            "bootstrap failed while contacting api.internal.example: "
            "temporary failure in name resolution"
        ),
    )

    assert classification is None


@pytest.mark.unit
def test_setup_dependency_network_classifier_skips_wrapper_uv_argument_dns_failure() -> None:
    classification = _classify_setup_dependency_network_failure(
        command="./bootstrap.sh uv sync",
        returncode=1,
        stdout="",
        stderr=(
            "bootstrap failed while contacting api.internal.example: "
            "temporary failure in name resolution"
        ),
    )

    assert classification is None


@pytest.mark.unit
def test_setup_dependency_network_classifier_skips_chained_bootstrap_dns_failure() -> None:
    classification = _classify_setup_dependency_network_failure(
        command="python -m pip install -r requirements.txt && ./bootstrap",
        returncode=1,
        stdout="Requirement already satisfied: pytest\n",
        stderr=(
            "bootstrap failed while contacting api.internal.example: "
            "temporary failure in name resolution"
        ),
    )

    assert classification is None


@pytest.mark.unit
def test_setup_dependency_network_classifier_skips_multiline_bootstrap_dns_failure() -> None:
    classification = _classify_setup_dependency_network_failure(
        command="python -m pip install -r requirements.txt\n./bootstrap",
        returncode=1,
        stdout="Requirement already satisfied: pytest\n",
        stderr=(
            "bootstrap failed while contacting api.internal.example: "
            "temporary failure in name resolution"
        ),
    )

    assert classification is None


@pytest.mark.unit
def test_setup_dependency_network_classifier_skips_chained_bootstrap_fetch_failure() -> None:
    classification = _classify_setup_dependency_network_failure(
        command="python -m pip install -r requirements.txt && ./bootstrap",
        returncode=1,
        stdout="Requirement already satisfied: pytest\n",
        stderr="bootstrap failed to fetch config: connection timed out",
    )

    assert classification is None


@pytest.mark.unit
def test_setup_dependency_network_classifier_skips_chained_bootstrap_after_package_output() -> None:
    classification = _classify_setup_dependency_network_failure(
        command="python -m pip install docker==7.1.0 && ./bootstrap",
        returncode=1,
        stdout="Collecting docker==7.1.0\nSuccessfully installed docker-7.1.0\n",
        stderr="bootstrap failed while contacting api.internal.example: connection timed out",
    )

    assert classification is None


@pytest.mark.unit
def test_setup_dependency_network_classifier_skips_standalone_bootstrap_fetch_failure() -> None:
    classification = _classify_setup_dependency_network_failure(
        command="./bootstrap",
        returncode=1,
        stdout="",
        stderr="bootstrap failed to fetch config: connection timed out",
    )

    assert classification is None


@pytest.mark.unit
def test_setup_dependency_network_classifier_skips_unknown_wrapper_after_successful_package_output() -> (
    None
):
    classification = _classify_setup_dependency_network_failure(
        command="./setup.sh",
        returncode=1,
        stdout=(
            "Collecting docker==7.1.0\n"
            "Installing collected packages: docker\n"
            "Successfully installed docker-7.1.0\n"
        ),
        stderr=(
            "bootstrap failed while contacting api.internal.example: "
            "temporary failure in name resolution"
        ),
    )

    assert classification is None


@pytest.mark.unit
def test_setup_dependency_network_classifier_skips_assignment_like_fetch_failure() -> None:
    classification = _classify_setup_dependency_network_failure(
        command="./bootstrap",
        returncode=1,
        stdout="",
        stderr=(
            "bootstrap failed to fetch CONFIG_URL=https://api.internal.example/config: "
            "connection timed out"
        ),
    )

    assert classification is None


@pytest.mark.unit
def test_setup_dependency_network_classifier_accepts_chained_dependency_output() -> None:
    classification = _classify_setup_dependency_network_failure(
        command="python -m pip install -r requirements.txt && ./bootstrap",
        returncode=1,
        stdout="",
        stderr=(
            "Failed to download docker==7.1.0 from https://files.pythonhosted.org/simple: "
            "temporary failure in name resolution"
        ),
    )

    assert classification is not None
    assert classification.reason_code == SETUP_DEPENDENCY_NETWORK_FAILURE
    assert classification.package == "docker==7.1.0"
    assert classification.host == "files.pythonhosted.org"


@pytest.mark.unit
def test_setup_dependency_network_metadata_allows_host_without_package() -> None:
    classification = _classify_setup_dependency_network_failure(
        command="pip install -r requirements.txt",
        returncode=1,
        stdout="",
        stderr=(
            "Package index https://files.pythonhosted.org/simple returned HTTP status code 503"
        ),
    )

    assert classification is not None
    assert classification.package is None
    assert classification.host == "files.pythonhosted.org"
    assert "package" not in classification.metadata
    assert classification.metadata["host"] == "files.pythonhosted.org"


@pytest.mark.unit
def test_setup_dependency_network_classifier_accepts_chained_multiline_dependency_output() -> None:
    classification = _classify_setup_dependency_network_failure(
        command="uv sync --extra dev && ./bootstrap",
        returncode=1,
        stdout="",
        stderr=_uv_pypi_dns_failure(),
    )

    assert classification is not None
    assert classification.reason_code == SETUP_DEPENDENCY_NETWORK_FAILURE
    assert classification.transient_category == "dns"
    assert classification.package == "docker==7.1.0"
    assert classification.host == "files.pythonhosted.org"


@pytest.mark.unit
@pytest.mark.parametrize(
    "command",
    [
        "npm run build",
        "poetry run python scripts/bootstrap.py",
        "bundle exec rake assets:precompile",
        "go test ./...",
    ],
)
def test_setup_dependency_network_classifier_skips_non_install_package_manager_verbs(
    command: str,
) -> None:
    classification = _classify_setup_dependency_network_failure(
        command=command,
        returncode=1,
        stdout="",
        stderr="setup command failed: temporary failure in name resolution",
    )

    assert classification is None


@pytest.mark.unit
def test_setup_dependency_network_classifier_does_not_use_command_for_context_fallback() -> None:
    classification = _classify_setup_dependency_network_failure(
        command="./sync-packages.sh",
        returncode=1,
        stdout="",
        stderr="setup command failed: temporary failure in name resolution",
    )

    assert classification is None


@pytest.mark.unit
def test_setup_dependency_network_classifier_ignores_plain_simple_context_fallback() -> None:
    classification = _classify_setup_dependency_network_failure(
        command="./build.sh",
        returncode=1,
        stdout="",
        stderr="simple error: temporary failure in name resolution",
    )

    assert classification is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "command",
    [
        "npm ci",
        "poetry install",
        "bundle install",
        "go install example.com/acme/tool@latest",
        "gradle --no-daemon dependencies",
        "mvn -B -DskipTests dependency:go-offline",
        "python -m pip install -r requirements.txt",
    ],
)
def test_setup_dependency_network_classifier_accepts_install_package_manager_verbs(
    command: str,
) -> None:
    classification = _classify_setup_dependency_network_failure(
        command=command,
        returncode=1,
        stdout="",
        stderr="setup command failed: temporary failure in name resolution",
    )

    assert classification is not None
    assert classification.reason_code == SETUP_DEPENDENCY_NETWORK_FAILURE
    assert classification.transient_category == "dns"


@pytest.mark.unit
@pytest.mark.parametrize(
    "command",
    [
        "pip --cache-dir /tmp/pip install -r requirements.txt",
        "pip --proxy http://proxy:8080 install -r requirements.txt",
        "pip --log /tmp/pip.log install -r requirements.txt",
        "pip --retries 2 install -r requirements.txt",
        "pip --exists-action w install -r requirements.txt",
        "pip --keyring-provider disabled install -r requirements.txt",
        "pip --resume-retries 2 install -r requirements.txt",
        "pip --use-feature fast-deps install -r requirements.txt",
        "pip --cert /etc/ssl/corp.pem install -r requirements.txt",
        "python -m pip --client-cert client.pem install -r requirements.txt",
    ],
)
def test_setup_dependency_network_classifier_accepts_pip_value_flags_before_subcommand(
    command: str,
) -> None:
    classification = _classify_setup_dependency_network_failure(
        command=command,
        returncode=1,
        stdout="",
        stderr="setup command failed: temporary failure in name resolution",
    )

    assert classification is not None
    assert classification.reason_code == SETUP_DEPENDENCY_NETWORK_FAILURE
    assert classification.transient_category == "dns"


@pytest.mark.unit
@pytest.mark.parametrize(
    "command",
    [
        "npm --workspace apps/console ci",
        "npm -w apps/console ci",
    ],
)
def test_setup_dependency_network_classifier_accepts_npm_workspace_flags_before_subcommand(
    command: str,
) -> None:
    classification = _classify_setup_dependency_network_failure(
        command=command,
        returncode=1,
        stdout="",
        stderr="setup command failed: temporary failure in name resolution",
    )

    assert classification is not None
    assert classification.reason_code == SETUP_DEPENDENCY_NETWORK_FAILURE
    assert classification.transient_category == "dns"


@pytest.mark.unit
@pytest.mark.parametrize(
    "command",
    [
        "pnpm --filter apps/console install",
        "pnpm -F apps/console install",
    ],
)
def test_setup_dependency_network_classifier_accepts_pnpm_filter_flags_before_subcommand(
    command: str,
) -> None:
    classification = _classify_setup_dependency_network_failure(
        command=command,
        returncode=1,
        stdout="",
        stderr="setup command failed: temporary failure in name resolution",
    )

    assert classification is not None
    assert classification.reason_code == SETUP_DEPENDENCY_NETWORK_FAILURE
    assert classification.transient_category == "dns"


@pytest.mark.unit
def test_setup_dependency_network_classifier_accepts_pnpm_dir_flag_before_subcommand() -> None:
    classification = _classify_setup_dependency_network_failure(
        command="pnpm --dir apps/console install",
        returncode=1,
        stdout="",
        stderr="setup command failed: temporary failure in name resolution",
    )

    assert classification is not None
    assert classification.reason_code == SETUP_DEPENDENCY_NETWORK_FAILURE
    assert classification.transient_category == "dns"


@pytest.mark.unit
@pytest.mark.parametrize(
    "command",
    [
        "yarn --immutable",
        "yarn --immutable --immutable-cache",
        "yarn --cwd apps/console --immutable",
    ],
)
def test_setup_dependency_network_classifier_accepts_yarn_option_only_installs(
    command: str,
) -> None:
    classification = _classify_setup_dependency_network_failure(
        command=command,
        returncode=1,
        stdout="",
        stderr="setup command failed: temporary failure in name resolution",
    )

    assert classification is not None
    assert classification.reason_code == SETUP_DEPENDENCY_NETWORK_FAILURE
    assert classification.transient_category == "dns"


@pytest.mark.unit
@pytest.mark.parametrize(
    "command",
    [
        "yarn --version",
        "yarn --help",
        "yarn --immutable --help",
    ],
)
def test_setup_dependency_network_classifier_skips_yarn_non_install_options(
    command: str,
) -> None:
    classification = _classify_setup_dependency_network_failure(
        command=command,
        returncode=1,
        stdout="",
        stderr="setup command failed: temporary failure in name resolution",
    )

    assert classification is None


@pytest.mark.unit
@pytest.mark.parametrize(
    ("command", "stderr", "expected_category", "expected_package", "expected_host"),
    [
        (
            "npm ci",
            "npm ERR! request to "
            "https://registry.npmjs.org/left-pad/-/left-pad-1.3.0.tgz "
            "failed, reason: getaddrinfo EAI_AGAIN registry.npmjs.org",
            "dns",
            "left-pad==1.3.0",
            "registry.npmjs.org",
        ),
        (
            "npm ci",
            "npm ERR! request to https://registry.npmjs.org/react/-/react-18.2.0.tgz "
            "failed, reason: getaddrinfo ENOTFOUND registry.npmjs.org",
            "dns",
            "react==18.2.0",
            "registry.npmjs.org",
        ),
        (
            "pnpm install --frozen-lockfile",
            "ERR_PNPM_FETCH_ request to "
            "https://registry.npmjs.org/is-odd/-/is-odd-3.0.1.tgz "
            "failed, reason: connect ETIMEDOUT 104.16.25.34:443",
            "connect_timeout",
            "is-odd==3.0.1",
            "registry.npmjs.org",
        ),
        (
            "yarn install --frozen-lockfile",
            "error Error: https://registry.yarnpkg.com/lodash/-/lodash-4.17.21.tgz: "
            "read ECONNRESET",
            "connection",
            "lodash==4.17.21",
            "registry.yarnpkg.com",
        ),
        (
            "npm ci",
            "npm ERR! request to https://registry.npmjs.org/react/-/react-18.2.0.tgz "
            "failed, reason: connect ECONNREFUSED registry.npmjs.org:443",
            "connection",
            "react==18.2.0",
            "registry.npmjs.org",
        ),
        (
            "pnpm install --frozen-lockfile",
            "ERR_PNPM_PREPARE_PKG_FAILURE Command failed: git ls-remote --refs "
            "https://github.com/acme/private-dep.git\n"
            "fatal: unable to access 'https://github.com/acme/private-dep.git/': "
            "Could not resolve host: github.com",
            "dns",
            None,
            "github.com",
        ),
    ],
)
def test_setup_dependency_network_classifier_accepts_node_transient_error_codes(
    command: str,
    stderr: str,
    expected_category: str,
    expected_package: str | None,
    expected_host: str,
) -> None:
    classification = _classify_setup_dependency_network_failure(
        command=command,
        returncode=1,
        stdout="",
        stderr=stderr,
    )

    assert classification is not None
    assert classification.reason_code == SETUP_DEPENDENCY_NETWORK_FAILURE
    assert classification.retryable is True
    assert classification.transient_category == expected_category
    assert classification.package == expected_package
    assert classification.host == expected_host


@pytest.mark.unit
def test_setup_dependency_network_classifier_accepts_go_mod_download_proxy_failure() -> None:
    classification = _classify_setup_dependency_network_failure(
        command="go mod download",
        returncode=1,
        stdout="",
        stderr=(
            "go: golang.org/x/text@v0.3.7: Get "
            '"https://proxy.golang.org/golang.org/x/text/@v/v0.3.7.mod": '
            "dial tcp: lookup proxy.golang.org: temporary failure in name resolution"
        ),
    )

    assert classification is not None
    assert classification.reason_code == SETUP_DEPENDENCY_NETWORK_FAILURE
    assert classification.retryable is True
    assert classification.transient_category == "dns"
    assert classification.host == "proxy.golang.org"


@pytest.mark.unit
def test_setup_dependency_network_classifier_ignores_unrelated_5xx_numbers() -> None:
    classification = _classify_setup_dependency_network_failure(
        command="uv sync --extra dev",
        returncode=137,
        stdout="installing docker==7.1.0 from local cache\n",
        stderr="dependency setup worker exited with code 512 after OOM kill\n",
    )

    assert classification is None


@pytest.mark.unit
def test_setup_dependency_network_classifier_ignores_non_http_status_code_5xx() -> None:
    classification = _classify_setup_dependency_network_failure(
        command="uv sync --extra dev",
        returncode=1,
        stdout="installing docker==7.1.0 from local cache\n",
        stderr="dependency setup worker reported status code 512 after local process crash\n",
    )

    assert classification is None


@pytest.mark.unit
def test_setup_dependency_network_classifier_ignores_bare_5xx_phrase_without_http_context() -> None:
    classification = _classify_setup_dependency_network_failure(
        command="uv sync --extra dev",
        returncode=1,
        stdout="installing docker==7.1.0 from local cache\n",
        stderr="dependency setup worker reported service unavailable after pool shutdown\n",
    )

    assert classification is None


@pytest.mark.unit
def test_setup_dependency_network_classifier_retries_503_temporarily_forbidden_body() -> None:
    classification = _classify_setup_dependency_network_failure(
        command="pip install docker==7.1.0",
        returncode=1,
        stdout="",
        stderr=(
            "Package index https://files.pythonhosted.org/simple returned HTTP status code 503: "
            "access temporarily forbidden by rate limit while fetching docker==7.1.0"
        ),
    )

    assert classification is not None
    assert classification.reason_code == SETUP_DEPENDENCY_NETWORK_FAILURE
    assert classification.retryable is True
    assert classification.transient_category == "http_5xx"
    assert classification.package == "docker==7.1.0"
    assert classification.host == "files.pythonhosted.org"


@pytest.mark.unit
def test_setup_dependency_network_classifier_skips_success_and_missing_tool() -> None:
    transient_output = "failed to download docker==7.1.0: connection timed out"

    assert (
        _classify_setup_dependency_network_failure(
            command="pip install docker==7.1.0",
            returncode=0,
            stdout="",
            stderr=transient_output,
        )
        is None
    )
    assert (
        _classify_setup_dependency_network_failure(
            command="pip install docker==7.1.0",
            returncode=127,
            stdout="",
            stderr=transient_output,
        )
        is None
    )


@pytest.mark.unit
def test_setup_dependency_context_helpers_accept_index_evidence() -> None:
    assert validation_module._setup_dependency_output_has_specific_context(  # noqa: SLF001
        "Package index /simple returned HTTP status code 503"
    )
    assert validation_module._setup_dependency_output_has_specific_context(  # noqa: SLF001
        "failed to fetch https://files.pythonhosted.org/packages/cu121/torch.whl"
    )
    assert validation_module._setup_dependency_output_has_specific_context(  # noqa: SLF001
        "failed fetching files.pythonhosted.org after retries"
    )
    assert validation_module._setup_dependency_output_has_specific_transient_context(  # noqa: SLF001
        "\n".join(
            [
                "Collecting docker==7.1.0",
                "noise",
                "failed to fetch dependency from registry.npmjs.org: connection timed out",
            ]
        )
    )
    assert validation_module._is_setup_dependency_index_host(None) is False  # noqa: SLF001
    assert validation_module._is_setup_dependency_index_host("mirror.pypi.org")  # noqa: SLF001


@pytest.mark.unit
def test_setup_dependency_shell_compound_detection_handles_quotes_and_parse_errors() -> None:
    assert validation_module._has_unquoted_shell_newline("pip install a\npytest")  # noqa: SLF001
    assert not validation_module._has_unquoted_shell_newline("echo 'a\nb'")  # noqa: SLF001
    assert not validation_module._has_unquoted_shell_newline('echo "a\nb"')  # noqa: SLF001
    assert not validation_module._has_unquoted_shell_newline("echo \\\ncontinued")  # noqa: SLF001
    assert not validation_module._has_shell_compound_control_operator("'unterminated")  # noqa: SLF001


@pytest.mark.unit
def test_setup_dependency_command_match_defensive_edges() -> None:
    assert validation_module._direct_dependency_setup_command_match([], start=0) is None  # noqa: SLF001
    assert (  # noqa: SLF001
        validation_module._direct_dependency_setup_command_match(["custom"], start=0) is None
    )
    assert validation_module._direct_dependency_setup_command_match(["pip"], start=0) is True  # noqa: SLF001
    assert (  # noqa: SLF001
        validation_module._direct_dependency_setup_command_match(["go", "mod"], start=0) is False
    )
    assert (  # noqa: SLF001
        validation_module._direct_dependency_setup_command_match(
            ["go", "mod", "download"],
            start=0,
        )
        is True
    )
    assert (
        validation_module._option_only_dependency_install_command_match(  # noqa: SLF001
            ["yarn", "--cwd"],
            command="yarn",
            start=1,
        )
        is False
    )
    assert (
        validation_module._option_only_dependency_install_command_match(  # noqa: SLF001
            ["yarn", "--"],
            command="yarn",
            start=1,
        )
        is False
    )
    assert (
        validation_module._option_only_dependency_install_command_match(  # noqa: SLF001
            ["pip", "--immutable"],
            command="pip",
            start=1,
        )
        is False
    )


@pytest.mark.unit
def test_setup_dependency_python_and_uv_command_match_defensive_edges() -> None:
    assert (  # noqa: SLF001
        validation_module._python_module_pip_dependency_setup_command_match(
            ["not-python", "-m", "pip"],
            start=0,
        )
        is None
    )
    assert (  # noqa: SLF001
        validation_module._python_module_pip_dependency_setup_command_match(
            ["python"],
            start=1,
        )
        is None
    )
    assert (  # noqa: SLF001
        validation_module._python_module_pip_dependency_setup_command_match(
            ["python", "-m"], start=0
        )
        is False
    )
    assert (  # noqa: SLF001
        validation_module._python_module_pip_dependency_setup_command_match(
            ["python", "-m", "venv"],
            start=0,
        )
        is None
    )
    assert (  # noqa: SLF001
        validation_module._python_module_pip_dependency_setup_command_match(
            ["python", "script.py"],
            start=0,
        )
        is None
    )
    assert (  # noqa: SLF001
        validation_module._python_module_pip_dependency_setup_command_match(
            ["python", "-I", "-B"],
            start=0,
        )
        is None
    )
    assert not validation_module._looks_like_uv_dependency_setup_command([])  # noqa: SLF001
    assert not validation_module._looks_like_uv_dependency_setup_command(["uv"])  # noqa: SLF001
    assert not validation_module._looks_like_uv_dependency_setup_command(["uv", "pip"])  # noqa: SLF001
    assert validation_module._looks_like_uv_dependency_setup_command(  # noqa: SLF001
        ["uv", "tool", "install", "ruff"]
    )
    assert (
        validation_module._next_dependency_tool_subcommand_index(  # noqa: SLF001
            ["pip", "--", "install"],
            start=1,
        )
        == 2
    )
    assert (
        validation_module._next_dependency_tool_subcommand_index(  # noqa: SLF001
            ["pip", "--"],
            start=1,
        )
        is None
    )
    assert validation_module._next_uv_subcommand_index(["uv", "--"], start=1) is None  # noqa: SLF001


@pytest.mark.unit
def test_setup_dependency_python_and_uv_option_value_edges() -> None:
    assert (  # noqa: SLF001
        validation_module._python_module_pip_dependency_setup_command_match(
            ["python", "--", "-m", "pip"],
            start=0,
        )
        is None
    )
    assert (  # noqa: SLF001
        validation_module._python_module_pip_dependency_setup_command_match(
            ["python", "-X", "dev", "-m", "pip", "install"],
            start=0,
        )
        is True
    )
    assert (
        validation_module._next_uv_subcommand_index(  # noqa: SLF001
            ["uv", "--extra", "dev", "sync"],
            start=1,
        )
        == 3
    )


@pytest.mark.unit
def test_setup_dependency_url_userinfo_stripping_handles_ipv6_and_bad_ports() -> None:
    ipv6_match = validation_module._SETUP_URL_RE.search(  # noqa: SLF001
        "https://user:pass@[2001:db8::1]:8443/simple/docker-7.1.0.whl"
    )
    assert ipv6_match is not None
    assert (
        validation_module._strip_setup_dependency_url_userinfo(ipv6_match)  # noqa: SLF001
        == "https://[2001:db8::1]:8443/simple/docker-7.1.0.whl"
    )

    bad_port_match = validation_module._SETUP_URL_RE.search(  # noqa: SLF001
        "https://user:pass@example.com:bad/simple/docker-7.1.0.whl"
    )
    assert bad_port_match is not None
    assert (
        validation_module._strip_setup_dependency_url_userinfo(bad_port_match)  # noqa: SLF001
        == "https://example.com/simple/docker-7.1.0.whl"
    )


@pytest.mark.unit
def test_setup_dependency_retry_prefix_and_file_read_edges(tmp_path: Path) -> None:
    runner = ValidationRunner(
        runner=FakeCommandRunner(),
        artifacts_dir=tmp_path / "artifacts",
        setup_retry_backoff_seconds=(),
    )

    assert runner._setup_dependency_retry_delay(1) == 0.0  # noqa: SLF001
    assert (
        validation_module._setup_dependency_retry_output_prefix(retry_number=2)  # noqa: SLF001
        == "\n[setup dependency network retry 2]\n"
    )

    class _UnreadablePath:
        def is_file(self) -> bool:
            return True

        def read_text(self, *, encoding: str, errors: str) -> str:
            assert encoding == "utf-8"
            assert errors == "replace"
            raise OSError("permission denied")

    assert validation_module._read_text_if_present(_UnreadablePath()) is None  # type: ignore[arg-type]  # noqa: SLF001


@pytest.mark.unit
def test_pytest_failure_text_helpers_cover_file_level_and_truncation() -> None:
    assert (
        validation_module._pytest_node_id_from_text(  # noqa: SLF001
            "tests/unit/test_app.py::",
            allow_file_level=True,
        )
        is None
    )
    assert (
        validation_module._pytest_node_id_from_text("tests/unit/test_app.py failed")  # noqa: SLF001
        is None
    )
    assert (
        validation_module._pytest_node_id_from_text(  # noqa: SLF001
            "tests/unit/test_app.py failed",
            allow_file_level=True,
        )
        == "tests/unit/test_app.py"
    )
    assert (
        validation_module._strip_pytest_node_id_suffix(  # noqa: SLF001
            "tests/unit/test_app.py::test_case:"
        )
        == "tests/unit/test_app.py::test_case"
    )
    assert validation_module._truncate_pytest_evidence_line("short") == "short"  # noqa: SLF001
    long_line = "x" * (validation_module._PYTEST_EVIDENCE_MAX_CHARS + 10)  # noqa: SLF001
    assert validation_module._truncate_pytest_evidence_line(long_line).endswith("...")  # noqa: SLF001


@pytest.mark.unit
def test_setup_dependency_network_classifier_retries_5xx_phrase_with_http_context() -> None:
    classification = _classify_setup_dependency_network_failure(
        command="pip install docker==7.1.0",
        returncode=1,
        stdout="",
        stderr=(
            "Package index https://files.pythonhosted.org/simple returned 503 Service Unavailable "
            "while fetching docker==7.1.0"
        ),
    )

    assert classification is not None
    assert classification.reason_code == SETUP_DEPENDENCY_NETWORK_FAILURE
    assert classification.retryable is True
    assert classification.transient_category == "http_5xx"
    assert classification.package == "docker==7.1.0"
    assert classification.host == "files.pythonhosted.org"


@pytest.mark.unit
def test_setup_dependency_network_classifier_retries_dependency_simple_index_fallback() -> None:
    classification = _classify_setup_dependency_network_failure(
        command="./bootstrap-deps.sh",
        returncode=1,
        stdout="",
        stderr=(
            "Failed to download docker==7.1.0 from https://files.pythonhosted.org/simple/: "
            "temporary failure in name resolution"
        ),
    )

    assert classification is not None
    assert classification.reason_code == SETUP_DEPENDENCY_NETWORK_FAILURE
    assert classification.transient_category == "dns"
    assert classification.package == "docker==7.1.0"
    assert classification.host == "files.pythonhosted.org"


@pytest.mark.unit
@pytest.mark.parametrize("port", ["401", "403"])
def test_setup_dependency_network_classifier_does_not_treat_index_port_as_http_auth_status(
    port: str,
) -> None:
    classification = _classify_setup_dependency_network_failure(
        command=f"pip install docker==7.1.0 --index-url http://pypi.internal:{port}/simple/",
        returncode=1,
        stdout="",
        stderr=(
            f"Failed to download docker==7.1.0 from http://pypi.internal:{port}/simple/: "
            "temporary failure in name resolution"
        ),
    )

    assert classification is not None
    assert classification.reason_code == SETUP_DEPENDENCY_NETWORK_FAILURE
    assert classification.transient_category == "dns"
    assert classification.package == "docker==7.1.0"
    assert classification.host == "pypi.internal"


@pytest.mark.unit
def test_setup_dependency_network_classifier_keeps_403_forbidden_deterministic() -> None:
    classification = _classify_setup_dependency_network_failure(
        command="pip install docker==7.1.0",
        returncode=1,
        stdout="",
        stderr=(
            "Package index https://files.pythonhosted.org/simple returned HTTP status code 403 "
            "Forbidden while fetching docker==7.1.0: temporary failure in name resolution"
        ),
    )

    assert classification is None


@pytest.mark.unit
@pytest.mark.parametrize("status_code", ["401", "403"])
def test_setup_dependency_network_classifier_keeps_http_auth_status_deterministic(
    status_code: str,
) -> None:
    classification = _classify_setup_dependency_network_failure(
        command="pip install docker==7.1.0",
        returncode=1,
        stdout="",
        stderr=(
            f"Package index https://files.pythonhosted.org/simple returned HTTP status code "
            f"{status_code} while fetching docker==7.1.0: temporary failure in name resolution"
        ),
    )

    assert classification is None


@pytest.mark.unit
@pytest.mark.parametrize(
    ("stderr", "expected_category"),
    [
        (
            "Failed to download docker==7.1.0 from https://files.pythonhosted.org/simple "
            "after connection reset by peer",
            "connection",
        ),
        (
            "Failed to fetch package docker==7.1.0 from https://files.pythonhosted.org/simple: "
            "connection refused",
            "connection",
        ),
        (
            "Failed to download docker==7.1.0 from https://files.pythonhosted.org/simple: "
            "client error (Connect): tunnel error: unsuccessful",
            "connection",
        ),
        (
            "Failed to download docker==7.1.0 from https://files.pythonhosted.org/simple: "
            "connect timeout",
            "connect_timeout",
        ),
        (
            "Failed to fetch package docker==7.1.0 from https://files.pythonhosted.org/simple: "
            "read timed out",
            "read_timeout",
        ),
        (
            "Failed to download docker==7.1.0 from https://files.pythonhosted.org/simple: "
            "TLS handshake timeout",
            "tls",
        ),
        (
            "Package index https://files.pythonhosted.org/simple returned HTTP status code 503 "
            "while fetching docker==7.1.0",
            "http_5xx",
        ),
        (
            "Package index https://files.pythonhosted.org/simple returned HTTP/1.1 503 "
            "while fetching docker==7.1.0",
            "http_5xx",
        ),
    ],
)
def test_setup_dependency_network_classifier_covers_transient_shapes(
    stderr: str,
    expected_category: str,
) -> None:
    classification = _classify_setup_dependency_network_failure(
        command="pip install docker==7.1.0",
        returncode=1,
        stdout="",
        stderr=stderr,
    )

    assert classification is not None
    assert classification.reason_code == SETUP_DEPENDENCY_NETWORK_FAILURE
    assert classification.retryable is True
    assert classification.transient_category == expected_category
    assert classification.package == "docker==7.1.0"
    assert classification.host == "files.pythonhosted.org"


@pytest.mark.unit
async def test_setup_dependency_network_failure_retries_and_succeeds_on_cache_hit(
    tmp_path: Path,
) -> None:
    fake = FakeCommandRunner()
    val = ValidationRunner(
        runner=fake,
        artifacts_dir=tmp_path / "artifacts",
        setup_retry_backoff_seconds=(0,),
    )
    profile = WorkspaceProfile.model_validate(
        {
            "name": "setup-retry",
            "phases": {"setup": ["uv sync --extra dev"]},
        }
    )
    fake.queue_result(returncode=1, stderr=_uv_pypi_dns_failure())
    fake.queue_result(returncode=0, stdout="Using cached docker==7.1.0\n")

    result = await val.run_profile_phases(
        workspace_id="ws_setup_retry",
        compose_project=_COMPOSE_PROJECT,
        compose_file=_COMPOSE_FILE,
        profile=profile,
        phase_names=("setup",),
    )

    assert result.all_passed
    assert len(fake.calls) == 2
    command = result.commands[0]
    assert command.retry_count == 1
    retry_metadata = command.metadata["setup_dependency_network"]
    assert retry_metadata["reason_code"] == SETUP_DEPENDENCY_NETWORK_FAILURE
    assert retry_metadata["retry_count"] == 1
    assert retry_metadata["retry_exhausted"] is False
    assert retry_metadata["package"] == "docker==7.1.0"
    assert retry_metadata["host"] == "files.pythonhosted.org"
    stderr_text = command.stderr_path.read_text(encoding="utf-8")
    stdout_text = command.stdout_path.read_text(encoding="utf-8")
    retry_prefix_pattern = r"\[setup dependency network retry 1 at \d+(?:\.\d+)?s\]"
    assert re.search(retry_prefix_pattern, stdout_text)
    assert re.search(retry_prefix_pattern, stderr_text)
    assert _uv_pypi_dns_failure() in stderr_text
    assert "Using cached docker==7.1.0" in stdout_text


@pytest.mark.unit
async def test_setup_dependency_retry_does_not_consume_flaky_retry_budget(
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
            "validation": {"retry_budget": 1},
        }
    )
    fake.queue_result(returncode=1, stderr=_uv_pypi_dns_failure())
    fake.queue_result(returncode=124, stderr="command timed out")
    fake.queue_result(returncode=0, stdout="Using cached docker==7.1.0\n")

    result = await val.run_profile_phases(
        workspace_id="ws_setup_then_flaky_retry",
        compose_project=_COMPOSE_PROJECT,
        compose_file=_COMPOSE_FILE,
        profile=profile,
        phase_names=("setup",),
    )

    assert result.all_passed
    assert len(fake.calls) == 3
    command = result.commands[0]
    assert command.retry_count == 2
    retry_metadata = command.metadata["setup_dependency_network"]
    assert retry_metadata["retry_count"] == 1
    assert retry_metadata["setup_retry_count"] == 1
    assert retry_metadata["flaky_retry_count"] == 1
    assert retry_metadata["total_retry_count"] == 2
    assert retry_metadata["retry_budget"] == 2
    assert retry_metadata["retry_exhausted"] is False
    assert len(retry_metadata["attempts"]) == 1
    assert retry_metadata["attempts"][0]["attempt"] == 1
    assert retry_metadata["attempts"][0]["retry_number"] == 1


@pytest.mark.unit
async def test_optional_command_failure_is_advisory_and_does_not_fail_validation(
    tmp_path: Path,
) -> None:
    """A phase command with ``required: false`` must not block validation.

    Regression for PRRT_kwDOSJAM6s6IRgv7: the optional command's non-zero
    result was appended verbatim, so ``ValidationResult.all_passed`` (and
    ``first_failure``) still treated the workspace as failed even though the
    command was explicitly advisory.
    """
    fake = FakeCommandRunner()
    val = ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts")
    profile = WorkspaceProfile.model_validate(
        {
            "name": "optional-cmd",
            "phases": {
                "validate": [
                    {"command": "advisory-lint", "required": False},
                    "pytest -q",
                ]
            },
        }
    )
    fake.queue_result(returncode=1, stderr="advisory lint reported issues")
    fake.queue_result(returncode=0, stdout="all tests passed")

    result = await val.run_profile_phases(
        workspace_id="ws_optional_cmd",
        compose_project=_COMPOSE_PROJECT,
        compose_file=_COMPOSE_FILE,
        profile=profile,
        phase_names=("validate",),
    )

    # The advisory failure neither halts the sequence nor fails the verdict.
    assert result.all_passed
    assert result.first_failure is None
    assert len(fake.calls) == 2
    advisory = result.commands[0]
    assert advisory.returncode == 1
    assert advisory.required is False
    assert not advisory.ok
