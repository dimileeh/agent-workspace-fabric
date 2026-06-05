"""MCP tools for AWF first-run setup/start/init/client flows."""

from __future__ import annotations

import asyncio
import logging
import re
import shlex
from collections.abc import Iterable, Mapping
from pathlib import Path
from subprocess import CalledProcessError
from typing import Annotated, Any, cast

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult
from pydantic import Field

from awf.api.schemas import ErrorResponse
from awf.cli import first_run_mcp_bridge as _first_run_mcp_bridge
from awf.common.config import Settings
from awf.host_setup.clients import (
    AWF_MCP_SERVER_KEY,
    CLIENT_DESCRIPTORS,
    ClientConfigPlan,
    build_client_config_plan,
    normalize_clients,
)
from awf.host_setup.config import (
    ClientIntegrationConfig,
    HostSetupConfig,
    HostSetupConfigError,
    ProviderConfig,
    default_host_setup_config_path,
    read_host_setup_config,
)
from awf.host_setup.rendering import (
    CLIENT_CONFIG_CONFLICT,
    SETUP_CLIENT_UNKNOWN,
    SETUP_PROVIDER_UNKNOWN,
    SETUP_READINESS_FAILED,
    START_COMPOSE_ASSETS_MISSING,
    FirstRunIssue,
    FirstRunPayload,
    FirstRunRemediation,
    render_first_run_json,
)
from awf.host_setup.source_assets import (
    SOURCE_CHECKOUT_ASSETS_STALE,
    SOURCE_CHECKOUT_INVALID,
    SourceCheckoutError,
)
from awf.host_setup.system_checks import SetupCheckError
from awf.mcp.tool_result_types import SafeResult
from awf.profiles.onboarding import preview_project_onboarding, write_workspace_profile
from awf.service.bootstrap import (
    ServiceBootstrapError,
    ServiceBootstrapOptions,
    run_service_bootstrap,
)
from awf.service.provider_readiness import is_secret_env_key

_DEFAULT_START_TIMEOUT_SECONDS = _first_run_mcp_bridge.DEFAULT_START_TIMEOUT_SECONDS
_ClientEnvFileMissingError = _first_run_mcp_bridge.ClientEnvFileMissingError
_StartBootstrapInputs = _first_run_mcp_bridge.StartBootstrapInputs
_client_env_file_missing_payload = _first_run_mcp_bridge.client_env_file_missing_payload
_client_env = _first_run_mcp_bridge.client_setup_environ
_client_home = _first_run_mcp_bridge.client_setup_home
_client_now = _first_run_mcp_bridge.client_setup_now
_client_source_checkout_blocked_payload = (
    _first_run_mcp_bridge.client_source_checkout_blocked_payload
)
_client_which = _first_run_mcp_bridge.client_setup_which
_config_error_details = _first_run_mcp_bridge.setup_config_error_details
_existing_project_profile_path = _first_run_mcp_bridge.existing_project_profile_path
_init_project_onboarding_payload = _first_run_mcp_bridge.build_init_project_onboarding_payload
_reason_coded_payload = _first_run_mcp_bridge.setup_reason_coded_payload
_resolve_client_env_file = _first_run_mcp_bridge.resolve_client_env_file
_resolve_start_bootstrap_inputs = _first_run_mcp_bridge.resolve_start_bootstrap_inputs
_resolve_start_source_checkout = _first_run_mcp_bridge.resolve_start_source_checkout
_run_setup = _first_run_mcp_bridge.run_setup_readiness
_source_checkout_failure_payload = _first_run_mcp_bridge.source_checkout_failure_payload
_start_failure_payload = _first_run_mcp_bridge.start_failure_payload
_start_success_payload = _first_run_mcp_bridge.start_success_payload

StructuredToolResult = Annotated[CallToolResult, dict[str, Any]]

START_OPTIONS_INVALID = "START_OPTIONS_INVALID"
START_INPUT_RESOLUTION_FAILED = "START_INPUT_RESOLUTION_FAILED"
START_BOOTSTRAP_EXECUTION_FAILED = "START_BOOTSTRAP_EXECUTION_FAILED"
PROJECT_INIT_INVALID_PATH = "PROJECT_INIT_INVALID_PATH"
PROJECT_PROFILE_EXISTS = "PROJECT_PROFILE_EXISTS"
PROJECT_INIT_FAILED = "PROJECT_INIT_FAILED"
_LOGGER = logging.getLogger(__name__)
_START_REASON_CODED_CLIENT_VALUE_PATTERN = "|".join(
    re.escape(client) for client in sorted(CLIENT_DESCRIPTORS, key=len, reverse=True)
)
_START_REASON_CODED_PROVIDER_VALUE_PATTERN = (
    r"(?:<provider>|[A-Za-z0-9_][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_][A-Za-z0-9_-]*)*)"
)
_START_REASON_CODED_SETUP_COMMAND_PATTERN = re.compile(
    rf"\bawf setup(?:"
    rf"\s+--dry-run(?:\s+--provider\s+{_START_REASON_CODED_PROVIDER_VALUE_PATTERN})?"
    rf"|\s+--provider\s+{_START_REASON_CODED_PROVIDER_VALUE_PATTERN}"
    rf"|\s+--client(?:\s+(?:<client>|{_START_REASON_CODED_CLIENT_VALUE_PATTERN}))?"
    rf")?"
)
_START_REMEDIATION_COMMAND_PATTERN = re.compile(r"^awf start(?:\s|$)")
_SOURCE_CHECKOUT_REMEDIATION_COMMAND_PATTERN = re.compile(
    r"^awf setup\s+--source-checkout(?:\s|=|$)"
)
_SOURCE_CHECKOUT_REMEDIATION_REASON_CODES = frozenset(
    {SOURCE_CHECKOUT_INVALID, SOURCE_CHECKOUT_ASSETS_STALE}
)
_SETUP_STATUS_NEXT_STEP_COMMAND_PATTERN = re.compile(
    r"(?P<setup>\bawf setup --dry-run(?P<setup_suffix>[.,;):]|$|\s+to\b))"
    r"|(?P<start_source>\bawf start\s+--source-checkout(?:=|\s+)"
    r"(?:'[^']*'|\"[^\"]*\"|\S)+?"
    r"(?P<start_source_suffix>[.,;):](?=\s|$)|$|\s+to\b))"
    r"|(?P<start>\bawf start(?P<start_suffix>[.,;):]|$|\s+to\b))"
)


def register_setup_tools(
    mcp: FastMCP,
    safe_result: SafeResult,
    settings_value: Settings,
) -> None:
    """Register first-run setup/start/init/client MCP tools."""
    # Kept for the shared register_* signature; setup tools resolve config via
    # host-setup helpers instead of AWF Settings.
    _ = settings_value

    @mcp.tool(name="awf_get_setup_status")
    async def awf_get_setup_status(
        providers: list[str] | None = Field(
            default=None,
            description="Optional provider selectors to status-check with setup readiness.",
        ),
        source_checkout: str | None = Field(
            default=None,
            min_length=1,
            max_length=4096,
            description="Optional AWF source checkout path whose local-service assets should be checked.",
        ),
    ) -> StructuredToolResult:
        """Return first-run setup status and safe setup metadata."""
        return await asyncio.to_thread(
            _get_setup_status_result,
            safe_result=safe_result,
            providers=providers or [],
            source_checkout=source_checkout,
        )

    @mcp.tool(name="awf_start_local_service")
    async def awf_start_local_service(
        rebuild: bool = Field(
            default=False,
            description="Force a full local agent-runtime rebuild before startup.",
        ),
        skip_agent_runtime_build: bool = Field(
            default=False,
            description="Skip building the local agent-runtime image before startup.",
        ),
        timeout_seconds: float = Field(
            default=180.0,
            ge=0.0,
            le=3600.0,
            description="Maximum seconds to wait for local AWF Core readiness.",
        ),
        source_checkout: str | None = Field(
            default=None,
            min_length=1,
            max_length=4096,
            description="Optional verified AWF source checkout path for bootstrap assets.",
        ),
    ) -> StructuredToolResult:
        """Start local AWF Core through the existing bootstrap engine."""
        return await _start_local_service_result(
            safe_result=safe_result,
            rebuild=rebuild,
            skip_agent_runtime_build=skip_agent_runtime_build,
            timeout_seconds=timeout_seconds,
            source_checkout=source_checkout,
        )

    @mcp.tool(name="awf_initialize_project_profile")
    async def awf_initialize_project_profile(
        project_path: str = Field(
            ...,
            min_length=1,
            max_length=4096,
            description="Path to the project repository to inspect for AWF onboarding.",
        ),
        include_smoke_request: bool = Field(
            default=False,
            description="Include a smoke-workspace request payload in the preview.",
        ),
        write_profile: bool = Field(
            default=False,
            description="Write .awf/workspace.yml from the onboarding preview.",
        ),
        template: str = Field(
            default="auto",
            min_length=1,
            max_length=64,
            description="Project onboarding template override, or auto.",
        ),
        force: bool = Field(
            default=False,
            description="Overwrite an existing project-local AWF profile when writing.",
        ),
    ) -> StructuredToolResult:
        """Preview or write a project-local AWF workspace profile."""
        return await asyncio.to_thread(
            _initialize_project_profile_result,
            safe_result=safe_result,
            project_path=project_path,
            include_smoke_request=include_smoke_request,
            write_profile=write_profile,
            template=template,
            force=force,
        )

    @mcp.tool(name="awf_get_client_integration_instructions")
    async def awf_get_client_integration_instructions(
        clients: list[str] | None = Field(
            default=None,
            description="Optional MCP client selectors. Defaults to every supported client.",
        ),
        source_checkout: str | None = Field(
            default=None,
            min_length=1,
            max_length=4096,
            description="Optional AWF source checkout path used to resolve the MCP env-file path.",
        ),
    ) -> StructuredToolResult:
        """Return secret-free MCP client integration instructions."""
        # clients=None means all currently registered MCP clients; the default
        # grows automatically as CLIENT_DESCRIPTORS gains new entries.
        return await asyncio.to_thread(
            _client_integration_instructions_result,
            safe_result=safe_result,
            clients=list(CLIENT_DESCRIPTORS) if clients is None else clients,
            source_checkout=source_checkout,
        )


def _get_setup_status_result(
    *,
    safe_result: SafeResult,
    providers: list[str],
    source_checkout: str | None,
) -> CallToolResult:
    source_path = _resolve_client_source_checkout_path(source_checkout)
    config_path: Path | None = None
    try:
        readiness = _run_setup(
            providers=providers,
            dry_run=True,
            non_interactive=True,
            allow_plain_secrets=False,
            source_checkout=source_path,
        )
        try:
            config = read_host_setup_config()
        except HostSetupConfigError:
            if source_path is None:
                raise
            config = HostSetupConfig()
        else:
            try:
                config_path = default_host_setup_config_path()
            except HostSetupConfigError:
                if source_path is None:
                    raise
    except SetupCheckError as exc:
        return _first_run_result(
            safe_result,
            _setup_status_reason_coded_payload(
                exc.reason_code,
                str(exc),
                exc.details,
                providers=providers,
                source_checkout=source_path,
            ),
            is_error=True,
        )
    except HostSetupConfigError as exc:
        return _first_run_result(
            safe_result,
            _setup_status_reason_coded_payload(
                exc.reason_code,
                exc.message,
                _config_error_details(exc),
                providers=providers,
                source_checkout=source_path,
            ),
            is_error=True,
        )
    except (CalledProcessError, OSError, RuntimeError, ValueError) as exc:
        return _first_run_result(
            safe_result,
            _setup_status_reason_coded_payload(
                SETUP_READINESS_FAILED,
                "could not inspect local setup readiness",
                {"error_type": type(exc).__name__},
                providers=providers,
                source_checkout=source_path,
            ),
            is_error=True,
        )

    try:
        rendered = render_first_run_json(readiness)
        details = _mapping(rendered.get("details"))
        selected_providers = _list_of_strings(details.get("selected_providers"))
        setup_payload: dict[str, Any] = {
            "dry_run": True,
            "selected_providers": selected_providers,
            "checks": _safe_setup_checks(details.get("checks")),
            "plain_file_consent": config.consent.plain_file_secrets,
            "source_checkout_assets_consent": config.consent.source_checkout_assets,
        }
        if config_path is not None:
            setup_payload["config_path"] = str(config_path)
        payload = {
            "status": rendered.get("status", "unknown"),
            "command": _setup_status_command(
                rendered.get("command"),
                selected_providers=selected_providers,
                source_checkout=source_path,
            ),
            "summary": rendered.get("summary", ""),
            "reason_code": rendered.get("reason_code"),
            "setup": setup_payload,
            "providers": _provider_statuses(config.providers),
            "clients": _client_statuses(config.clients),
            "source_checkout": _setup_status_source_checkout(
                config,
                details,
                rendered.get("issues"),
                prefer_probed=source_path is not None,
            ),
            "issues": _setup_status_issues(rendered.get("issues")),
            "next_steps": _setup_status_next_steps(
                rendered.get("next_steps"),
                selected_providers=selected_providers,
                source_checkout=source_path,
            ),
        }
        is_error = payload["status"] in ("blocked", "failed")
    except Exception as exc:
        return _first_run_result(
            safe_result,
            _setup_status_reason_coded_payload(
                SETUP_READINESS_FAILED,
                "could not build setup status response",
                {"error_type": type(exc).__name__},
                providers=providers,
                source_checkout=source_path,
            ),
            is_error=True,
        )
    return safe_result(payload, is_error=is_error)


async def _start_local_service_result(
    *,
    safe_result: SafeResult,
    rebuild: bool,
    skip_agent_runtime_build: bool,
    timeout_seconds: float,
    source_checkout: str | None,
) -> CallToolResult:
    if rebuild and skip_agent_runtime_build:
        return _error_result(
            safe_result,
            START_OPTIONS_INVALID,
            "--rebuild cannot be combined with --skip-agent-runtime-build.",
            detail={"rebuild": rebuild, "skip_agent_runtime_build": skip_agent_runtime_build},
        )

    source_path = _resolve_client_source_checkout_path(source_checkout)
    try:
        inputs = await asyncio.to_thread(_resolve_start_bootstrap_inputs_for_mcp, source_path)
    except SetupCheckError as exc:
        return _first_run_result(
            safe_result,
            _start_payload_with_command(
                _reason_coded_payload(exc.reason_code, str(exc), exc.details),
                rebuild=rebuild,
                skip_agent_runtime_build=skip_agent_runtime_build,
                timeout_seconds=timeout_seconds,
                source_checkout=source_path,
            ),
            is_error=True,
        )
    except SourceCheckoutError as exc:
        return _first_run_result(
            safe_result,
            _start_payload_with_command(
                _source_checkout_failure_payload(exc),
                rebuild=rebuild,
                skip_agent_runtime_build=skip_agent_runtime_build,
                timeout_seconds=timeout_seconds,
                source_checkout=source_path,
                remediation_source_checkout=exc.root,
            ),
            is_error=True,
        )
    except (CalledProcessError, HostSetupConfigError, OSError, RuntimeError, ValueError) as exc:
        return _start_input_resolution_error_result(
            safe_result,
            exc,
            rebuild=rebuild,
            skip_agent_runtime_build=skip_agent_runtime_build,
            timeout_seconds=timeout_seconds,
            source_checkout=source_path,
        )

    options = ServiceBootstrapOptions(
        timeout_seconds=timeout_seconds,
        skip_agent_runtime_build=skip_agent_runtime_build,
        force_rebuild=rebuild,
    )
    try:
        result = await run_service_bootstrap(
            inputs.settings,
            options=options,
            compose_file=inputs.compose_file,
            env_file=inputs.compose_env_file,
            asset_root=inputs.asset_root,
            service_environ=inputs.service_env,
        )
    except ServiceBootstrapError as exc:
        return _first_run_result(
            safe_result,
            _start_payload_with_command(
                _start_failure_payload(exc, env_migration=inputs.env_migration),
                rebuild=rebuild,
                skip_agent_runtime_build=skip_agent_runtime_build,
                timeout_seconds=timeout_seconds,
                source_checkout=source_path,
            ),
            is_error=True,
            extra_secrets=_selected_start_secret_values(inputs),
        )
    except (CalledProcessError, OSError, RuntimeError, ValueError) as exc:
        return _start_bootstrap_path_error_result(
            safe_result,
            exc,
            env_migration=inputs.env_migration,
            rebuild=rebuild,
            skip_agent_runtime_build=skip_agent_runtime_build,
            timeout_seconds=timeout_seconds,
            source_checkout=source_path,
        )

    return _first_run_result(
        safe_result,
        _start_payload_with_command(
            _start_success_payload(
                inputs.settings,
                result,
                env_migration=inputs.env_migration,
            ),
            rebuild=rebuild,
            skip_agent_runtime_build=skip_agent_runtime_build,
            timeout_seconds=timeout_seconds,
            source_checkout=source_path,
        ),
    )


def _resolve_start_bootstrap_inputs_for_mcp(
    source_path: Path | None,
) -> _StartBootstrapInputs:
    verified = _resolve_start_source_checkout(source_path)
    return _resolve_start_bootstrap_inputs(verified)


def _selected_start_secret_values(inputs: _StartBootstrapInputs) -> tuple[str, ...]:
    """Return exact secrets from the resolved start environment."""
    return _unique_secret_values(
        (
            *_selected_start_settings_secret_values(inputs.settings),
            *(value for key, value in inputs.service_env.items() if is_secret_env_key(key)),
        )
    )


def _selected_start_settings_secret_values(settings: object) -> tuple[str, ...]:
    values: list[str] = []
    for field_name in ("api_token", "github_token"):
        value = getattr(settings, field_name, None)
        if isinstance(value, str):
            values.append(value)
    return tuple(values)


def _unique_secret_values(values: Iterable[str | None]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value and len(value) >= 4))


def _start_input_resolution_error_result(
    safe_result: SafeResult,
    exc: CalledProcessError | HostSetupConfigError | OSError | RuntimeError | ValueError,
    *,
    rebuild: bool,
    skip_agent_runtime_build: bool,
    timeout_seconds: float,
    source_checkout: Path | None = None,
) -> CallToolResult:
    command = _start_command(
        rebuild=rebuild,
        skip_agent_runtime_build=skip_agent_runtime_build,
        timeout_seconds=timeout_seconds,
        source_checkout=source_checkout,
    )
    issue = FirstRunIssue(
        reason_code=START_INPUT_RESOLUTION_FAILED,
        severity="blocked",
        remediation=FirstRunRemediation(
            problem="AWF could not resolve local service startup inputs.",
            cause=(
                "A local startup asset, host setup configuration, or source-checkout path "
                "could not be resolved."
            ),
            fix=f"Inspect the selected local-service inputs, then retry {command}.",
            docs_link="docs/MCP_SETUP.md",
            related_command=command,
        ),
        details={"error_type": type(exc).__name__},
    )
    return _first_run_result(
        safe_result,
        FirstRunPayload(
            status="blocked",
            command=command,
            summary="could not resolve local service startup inputs",
            reason_code=START_INPUT_RESOLUTION_FAILED,
            issues=(issue,),
        ),
        is_error=True,
    )


def _start_bootstrap_path_error_result(
    safe_result: SafeResult,
    exc: CalledProcessError | OSError | RuntimeError | ValueError,
    *,
    env_migration: object | None = None,
    rebuild: bool,
    skip_agent_runtime_build: bool,
    timeout_seconds: float,
    source_checkout: Path | None = None,
) -> CallToolResult:
    failure = ServiceBootstrapError(
        reason_code=START_BOOTSTRAP_EXECUTION_FAILED,
        message="could not execute local service bootstrap",
        stderr=f"error_type={type(exc).__name__}",
    )
    return _first_run_result(
        safe_result,
        _start_payload_with_command(
            _start_failure_payload(failure, env_migration=env_migration),
            rebuild=rebuild,
            skip_agent_runtime_build=skip_agent_runtime_build,
            timeout_seconds=timeout_seconds,
            source_checkout=source_checkout,
        ),
        is_error=True,
    )


def _start_payload_with_command(
    payload: FirstRunPayload,
    *,
    rebuild: bool,
    skip_agent_runtime_build: bool,
    timeout_seconds: float,
    source_checkout: Path | None,
    remediation_source_checkout: Path | None = None,
) -> FirstRunPayload:
    command = _start_command(
        rebuild=rebuild,
        skip_agent_runtime_build=skip_agent_runtime_build,
        timeout_seconds=timeout_seconds,
        source_checkout=source_checkout,
    )
    update: dict[str, Any] = {"command": command}
    if payload.command == "awf setup":
        update["next_steps"] = _start_reason_coded_next_steps(payload.next_steps, command=command)
    issue_source_checkout = (
        source_checkout if remediation_source_checkout is None else remediation_source_checkout
    )
    if payload.issues:
        update["issues"] = _start_issues_with_command(
            payload.issues,
            command=command,
            source_checkout=issue_source_checkout,
        )
    return payload.model_copy(update=update)


def _start_issues_with_command(
    issues: tuple[FirstRunIssue, ...],
    *,
    command: str,
    source_checkout: Path | None,
) -> tuple[FirstRunIssue, ...]:
    return tuple(
        _start_issue_with_command(issue, command=command, source_checkout=source_checkout)
        for issue in issues
    )


def _start_issue_with_command(
    issue: FirstRunIssue,
    *,
    command: str,
    source_checkout: Path | None,
) -> FirstRunIssue:
    remediation = issue.remediation
    if issue.reason_code == START_COMPOSE_ASSETS_MISSING and source_checkout is None:
        return issue
    if not _should_rewrite_start_issue_remediation(issue, source_checkout=source_checkout):
        return issue
    related_command = command
    if (
        source_checkout is not None
        and issue.reason_code in _SOURCE_CHECKOUT_REMEDIATION_REASON_CODES
        and _is_source_checkout_remediation_command(remediation.related_command)
    ):
        related_command = _setup_source_checkout_command(source_checkout)
    return issue.model_copy(
        update={"remediation": remediation.model_copy(update={"related_command": related_command})}
    )


def _should_rewrite_start_issue_remediation(
    issue: FirstRunIssue,
    *,
    source_checkout: Path | None,
) -> bool:
    related_command = issue.remediation.related_command
    if _is_start_remediation_command(related_command):
        return True
    return (
        source_checkout is not None
        and issue.reason_code in _SOURCE_CHECKOUT_REMEDIATION_REASON_CODES
        and _is_source_checkout_remediation_command(related_command)
    )


def _is_start_remediation_command(command: str | None) -> bool:
    return command is not None and _START_REMEDIATION_COMMAND_PATTERN.match(command) is not None


def _is_source_checkout_remediation_command(command: str | None) -> bool:
    return (
        command is not None
        and _SOURCE_CHECKOUT_REMEDIATION_COMMAND_PATTERN.match(command) is not None
    )


def _start_reason_coded_next_steps(
    next_steps: tuple[str, ...],
    *,
    command: str,
) -> tuple[str, ...]:
    return tuple(_start_reason_coded_next_step(step, command=command) for step in next_steps)


def _start_reason_coded_next_step(step: str, *, command: str) -> str:
    return _START_REASON_CODED_SETUP_COMMAND_PATTERN.sub(lambda _: command, step, count=1)


def _start_command(
    *,
    rebuild: bool,
    skip_agent_runtime_build: bool,
    timeout_seconds: float,
    source_checkout: Path | None,
) -> str:
    command = ["awf", "start"]
    if rebuild:
        command.append("--rebuild")
    if skip_agent_runtime_build:
        command.append("--skip-agent-runtime-build")
    if timeout_seconds != _DEFAULT_START_TIMEOUT_SECONDS:
        command.extend(["--timeout-seconds", str(timeout_seconds)])
    if source_checkout is not None:
        command.extend(["--source-checkout", str(source_checkout)])
    return shlex.join(command)


def _initialize_project_profile_result(
    *,
    safe_result: SafeResult,
    project_path: str,
    include_smoke_request: bool,
    write_profile: bool,
    template: str,
    force: bool,
) -> CallToolResult:
    repository = _resolve_project_init_path(project_path)
    try:
        if not repository.exists():
            return _project_init_path_error(safe_result, "project path does not exist", repository)
        if not repository.is_dir():
            return _project_init_path_error(
                safe_result,
                "project path is not a directory",
                repository,
            )
    except OSError:
        return _project_init_path_error(safe_result, "could not inspect project path", repository)

    try:
        existing_profile_path = _existing_project_profile_path(repository)
    except Exception:
        _LOGGER.exception(
            "could not probe existing project profile for MCP project initialization",
            extra={"project_path": str(repository), "template": template},
        )
        return _error_result(
            safe_result,
            PROJECT_INIT_FAILED,
            "could not probe existing project profile",
            detail={"project_path": str(repository), "template": template},
        )

    try:
        preview = preview_project_onboarding(
            repository,
            template=template,
            include_smoke_request=include_smoke_request,
        )
    except Exception:
        _LOGGER.exception(
            "could not build onboarding preview for MCP project initialization",
            extra={"project_path": str(repository), "template": template},
        )
        return _error_result(
            safe_result,
            PROJECT_INIT_FAILED,
            "could not build onboarding preview",
            detail={"project_path": str(repository), "template": template},
        )

    mode = "write" if write_profile else "preview"
    planned_written_path = repository / ".awf" / "workspace.yml" if write_profile else None
    if (
        planned_written_path is not None
        and existing_profile_path is None
        and planned_written_path.exists()
    ):
        existing_profile_path = planned_written_path
    if planned_written_path is not None and existing_profile_path is not None and not force:
        return _error_result(
            safe_result,
            PROJECT_PROFILE_EXISTS,
            "project profile already exists; pass force=true to overwrite",
            detail={"project_path": str(repository), "force": force},
        )

    try:
        payload = _init_project_onboarding_payload(
            preview=preview,
            existing_profile_path=existing_profile_path,
            written_path=planned_written_path,
            service_status={
                "service": "awf",
                "status": "not_checked",
                "source": "mcp",
            },
            doctor_status="not_checked",
            local_checks_ready=False,
            guided=False,
            mode=mode,
        )
    except Exception:
        _LOGGER.exception(
            "could not build project onboarding MCP payload",
            extra={"project_path": str(repository), "mode": mode},
        )
        return _error_result(
            safe_result,
            PROJECT_INIT_FAILED,
            "could not build onboarding payload",
            detail={"project_path": str(repository), "mode": mode},
        )

    if write_profile:
        try:
            written_path = write_workspace_profile(preview, force=force)
        except FileExistsError:
            message = (
                "project profile already exists; pass force=true to overwrite"
                if not force
                else "project profile already exists and could not be overwritten"
            )
            return _error_result(
                safe_result,
                PROJECT_PROFILE_EXISTS,
                message,
                detail={"project_path": str(repository), "force": force},
            )
        except (OSError, RuntimeError, ValueError) as exc:
            return _error_result(
                safe_result,
                PROJECT_INIT_FAILED,
                f"could not write project profile: {type(exc).__name__}",
                detail={"project_path": str(repository), "force": force},
            )
        except Exception:
            _LOGGER.exception(
                "could not write project profile for MCP project initialization",
                extra={"project_path": str(repository), "force": force},
            )
            return _error_result(
                safe_result,
                PROJECT_INIT_FAILED,
                "could not write project profile",
                detail={"project_path": str(repository), "force": force},
            )
        if planned_written_path is not None and written_path != planned_written_path:
            payload["written_path"] = str(written_path)
    return safe_result(cast(dict[str, Any], payload))


def _client_integration_instructions_result(
    *,
    safe_result: SafeResult,
    clients: list[str],
    source_checkout: str | None,
) -> CallToolResult:
    selected: list[str] = []
    source_path: Path | None = None
    try:
        selected = normalize_clients(clients)
        if not selected:
            empty_payload: dict[str, Any] = {
                "status": "success",
                "summary": _client_instructions_summary([], blocked=False),
                "clients": [],
                "next_steps": _client_instruction_next_steps(
                    [],
                    blocked=False,
                    source_checkout=None,
                ),
            }
            return safe_result(empty_payload)

        source_path = _resolve_client_source_checkout_path(source_checkout)
        env_file = _resolve_client_env_file(source_path, True)
        home = _client_home()
        env = _client_env()
        plans = [
            build_client_config_plan(
                client,
                env_file=env_file,
                home=home,
                which=_client_which,
                now=_client_now,
                env=env,
            )
            for client in selected
        ]
    except (SetupCheckError, HostSetupConfigError) as exc:
        details = (
            _config_error_details(exc) if isinstance(exc, HostSetupConfigError) else exc.details
        )
        return _first_run_result(
            safe_result,
            _client_instruction_reason_coded_payload(
                exc.reason_code,
                str(exc),
                details,
                requested_clients=clients,
                selected_clients=selected,
                source_checkout=source_path,
            ),
            is_error=True,
        )
    except SourceCheckoutError as exc:
        remediation_source_checkout = source_path if source_path is not None else exc.root
        blocked_payload = _client_source_checkout_blocked_payload_with_explicit_command(
            _client_source_checkout_blocked_payload(exc),
            selected_clients=selected,
            source_checkout=remediation_source_checkout,
        )
        return _first_run_result(
            safe_result,
            blocked_payload,
            is_error=True,
        )
    except _ClientEnvFileMissingError as exc:
        missing_env_source_checkout = _client_env_file_missing_source_checkout(
            source_path,
            exc.env_file,
        )
        blocked_payload = _client_env_file_missing_payload_with_explicit_command(
            _client_env_file_missing_payload(exc.env_file),
            selected_clients=selected,
            source_checkout=missing_env_source_checkout,
        )
        return _first_run_result(
            safe_result,
            blocked_payload,
            is_error=True,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        return _first_run_result(
            safe_result,
            _client_instruction_reason_coded_payload(
                SETUP_READINESS_FAILED,
                "could not inspect local client integration environment",
                {"error_type": type(exc).__name__},
                requested_clients=clients,
                selected_clients=selected,
                source_checkout=source_path,
            ),
            is_error=True,
        )
    except Exception as exc:
        return _first_run_result(
            safe_result,
            _client_instruction_reason_coded_payload(
                SETUP_READINESS_FAILED,
                "could not plan client integration instructions",
                {"error_type": type(exc).__name__},
                requested_clients=clients,
                selected_clients=selected,
                source_checkout=source_path,
            ),
            is_error=True,
        )

    try:
        blocked = [plan for plan in plans if plan.action == "conflict"]
        status = "blocked" if blocked else "success"
        payload: dict[str, Any] = {
            "status": status,
            "command": _client_instruction_command(selected, source_checkout=source_path),
            "summary": _client_instructions_summary(plans, blocked=bool(blocked)),
            "env_file": str(env_file),
            "clients": [
                _client_instruction_payload(plan, source_checkout=source_path) for plan in plans
            ],
            "next_steps": _client_instruction_next_steps(
                plans,
                blocked=bool(blocked),
                source_checkout=source_path,
            ),
        }
        is_error = bool(blocked)
        if blocked:
            payload["reason_code"] = CLIENT_CONFIG_CONFLICT
    except Exception as exc:
        return _first_run_result(
            safe_result,
            _client_instruction_reason_coded_payload(
                SETUP_READINESS_FAILED,
                "could not build client integration instructions",
                {"error_type": type(exc).__name__},
                requested_clients=clients,
                selected_clients=selected,
                source_checkout=source_path,
            ),
            is_error=True,
        )
    return safe_result(payload, is_error=is_error)


def _first_run_result(
    safe_result: SafeResult,
    payload: FirstRunPayload,
    *,
    is_error: bool = False,
    extra_secrets: Iterable[str] = (),
) -> CallToolResult:
    return safe_result(
        render_first_run_json(payload),
        is_error=is_error,
        extra_secrets=extra_secrets,
    )


def _error_result(
    safe_result: SafeResult,
    error_code: str,
    message: str,
    *,
    detail: dict[str, Any] | None = None,
) -> CallToolResult:
    error = ErrorResponse(error_code=error_code, message=message, detail=detail)
    return safe_result(error.model_dump(mode="json"), is_error=True)


def _project_init_path_error(
    safe_result: SafeResult,
    message: str,
    repository: Path,
) -> CallToolResult:
    return _error_result(
        safe_result,
        PROJECT_INIT_INVALID_PATH,
        message,
        detail={"project_path": str(repository)},
    )


def _provider_statuses(providers: Mapping[str, ProviderConfig]) -> dict[str, dict[str, Any]]:
    return {name: _provider_status(provider) for name, provider in providers.items()}


def _provider_status(provider: ProviderConfig) -> dict[str, Any]:
    payload: dict[str, Any] = {"status": provider.status}
    if provider.backend is not None:
        payload["backend"] = provider.backend
    if provider.source is not None:
        payload["source"] = provider.source
    payload["credential_ref"] = _credential_ref_metadata(provider.credential_ref)
    return payload


def _credential_ref_metadata(credential_ref: str | None) -> dict[str, Any]:
    if credential_ref is None:
        return {"present": False}
    scheme, separator, _rest = credential_ref.partition("://")
    payload: dict[str, Any] = {"present": True}
    if separator:
        payload["scheme"] = scheme
    return payload


def _client_statuses(clients: Mapping[str, ClientIntegrationConfig]) -> dict[str, dict[str, Any]]:
    statuses: dict[str, dict[str, Any]] = {}
    for name, client in clients.items():
        payload: dict[str, Any] = {"status": client.status}
        if client.updated_at is not None:
            payload["updated_at"] = client.updated_at.isoformat()
        statuses[name] = payload
    return statuses


def _source_checkout_status(config: HostSetupConfig) -> dict[str, Any]:
    if config.source_checkout is None:
        return {"present": False}
    return {
        "present": True,
        "root": str(config.source_checkout.root),
        "verified_at": config.source_checkout.verified_at.isoformat(),
        "marker_count": len(config.source_checkout.markers),
    }


def _setup_status_source_checkout(
    config: HostSetupConfig,
    details: Mapping[str, Any],
    issues: Any,
    *,
    prefer_probed: bool = False,
) -> dict[str, Any]:
    if _has_blocking_source_checkout_issue(issues):
        return {"present": False}

    probed = _probed_source_checkout_status(details)
    if prefer_probed:
        return probed

    persisted = _source_checkout_status(config)
    if persisted["present"]:
        return persisted

    return probed


def _probed_source_checkout_status(details: Mapping[str, Any]) -> dict[str, Any]:
    probed = _mapping(details.get("source_checkout"))
    root = probed.get("root")
    verified_at = probed.get("verified_at")
    if not isinstance(root, str) or not isinstance(verified_at, str):
        return {"present": False}

    payload: dict[str, Any] = {
        "present": True,
        "root": root,
        "verified_at": verified_at,
        # Dry-run probe metadata verifies the checkout but does not enumerate markers.
        "marker_count": None,
    }
    return payload


def _has_blocking_source_checkout_issue(value: Any) -> bool:
    if not isinstance(value, list):
        return False
    for item in value:
        item_mapping = _mapping(item)
        severity = item_mapping.get("severity")
        if severity not in ("blocked", "failed"):
            continue
        details = _mapping(item_mapping.get("details"))
        if details.get("check") == "source_checkout":
            return True
    return False


def _safe_setup_checks(value: Any) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []
    if not isinstance(value, list):
        return checks
    for item in value:
        item_mapping = _mapping(item)
        name = item_mapping.get("name")
        level = item_mapping.get("level")
        if isinstance(name, str) and isinstance(level, str):
            checks.append({"name": name, "level": level})
    return checks


def _setup_status_issues(value: Any) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    if not isinstance(value, list):
        return issues
    for item in value:
        item_mapping = _mapping(item)
        reason_code = item_mapping.get("reason_code")
        severity = item_mapping.get("severity")
        if not isinstance(reason_code, str) or not isinstance(severity, str):
            continue
        rendered: dict[str, Any] = {"reason_code": reason_code, "severity": severity}
        details = _mapping(item_mapping.get("details"))
        check = details.get("check")
        if isinstance(check, str):
            rendered["check"] = check
        issues.append(rendered)
    return issues


def _setup_status_command(
    _value: Any,
    *,
    selected_providers: list[str],
    source_checkout: Path | None,
) -> str:
    return _setup_status_dry_run_command(
        selected_providers=selected_providers,
        source_checkout=source_checkout,
    )


def _setup_status_reason_coded_payload(
    reason_code: str,
    summary: str,
    details: dict[str, Any],
    *,
    providers: list[str],
    source_checkout: Path | None,
) -> FirstRunPayload:
    payload = _reason_coded_payload(reason_code, summary, details)
    return payload.model_copy(
        update={
            "command": _setup_status_dry_run_command(
                selected_providers=providers,
                source_checkout=source_checkout,
            ),
            "next_steps": _setup_status_reason_coded_next_steps(
                reason_code,
                payload.next_steps,
                providers=providers,
                source_checkout=source_checkout,
            ),
        }
    )


def _setup_status_reason_coded_next_steps(
    reason_code: str,
    next_steps: tuple[str, ...],
    *,
    providers: list[str],
    source_checkout: Path | None,
) -> tuple[str, ...]:
    if reason_code == SETUP_PROVIDER_UNKNOWN:
        command = _setup_status_dry_run_command(
            selected_providers=[],
            source_checkout=source_checkout,
        )
        return (
            f"Re-run {command} with a supported --provider; the accepted names are "
            "listed under known_providers in the issue details.",
        )
    return tuple(
        _setup_status_next_steps(
            list(next_steps),
            selected_providers=providers,
            source_checkout=source_checkout,
        )
    )


def _setup_status_next_steps(
    value: Any,
    *,
    selected_providers: list[str],
    source_checkout: Path | None,
) -> list[str]:
    next_steps = _list_of_strings(value)
    setup_command = _setup_status_dry_run_command(
        selected_providers=selected_providers,
        source_checkout=source_checkout,
    )
    start_command = (
        _start_source_checkout_command(source_checkout) if source_checkout is not None else None
    )
    return [
        _setup_status_next_step_for_source_checkout(
            step,
            setup_command=setup_command,
            start_command=start_command,
        )
        for step in next_steps
    ]


def _setup_status_next_step_for_source_checkout(
    step: str,
    *,
    setup_command: str,
    start_command: str | None,
) -> str:
    def replace_command(match: re.Match[str]) -> str:
        if match.group("setup") is not None:
            return f"{setup_command}{match.group('setup_suffix') or ''}"
        if start_command is None:
            return match.group(0)
        if match.group("start_source") is not None:
            return f"{start_command}{match.group('start_source_suffix') or ''}"
        return f"{start_command}{match.group('start_suffix') or ''}"

    return _SETUP_STATUS_NEXT_STEP_COMMAND_PATTERN.sub(replace_command, step)


def _setup_status_dry_run_command(
    *,
    selected_providers: list[str],
    source_checkout: Path | None,
) -> str:
    command = ["awf", "setup", "--dry-run"]
    for provider in selected_providers:
        command.extend(["--provider", provider])
    if source_checkout is not None:
        command.extend(["--source-checkout", str(source_checkout)])
    return shlex.join(command)


def _start_source_checkout_command(source_checkout: Path) -> str:
    return shlex.join(["awf", "start", "--source-checkout", str(source_checkout)])


def _setup_source_checkout_command(source_checkout: Path) -> str:
    return shlex.join(["awf", "setup", "--source-checkout", str(source_checkout)])


def _client_instruction_payload(
    plan: ClientConfigPlan,
    *,
    source_checkout: Path | None,
) -> dict[str, Any]:
    descriptor = plan.descriptor or CLIENT_DESCRIPTORS[plan.client]
    desired_entry = dict(plan.desired_entry)
    payload: dict[str, Any] = {
        "client": plan.client,
        "label": descriptor.label,
        "config_path": str(plan.config_path),
        "action": plan.action,
        "method": plan.method,
        "desired_entry": desired_entry,
        "manual_config": {descriptor.servers_key: {AWF_MCP_SERVER_KEY: desired_entry}},
        "apply_command": _client_apply_command(plan.client, source_checkout=source_checkout),
    }
    if plan.cli_command is not None:
        payload["client_cli_command"] = list(plan.cli_command)
    if plan.conflict_detail is not None:
        payload["conflict_detail"] = plan.conflict_detail
    return payload


def _client_instructions_summary(plans: list[ClientConfigPlan], *, blocked: bool) -> str:
    if blocked:
        return "AWF found MCP client configuration conflicts while building instructions."
    if len(plans) == 1:
        return f"AWF MCP client instructions are ready for {plans[0].client}."
    return f"AWF MCP client instructions are ready for {len(plans)} clients."


def _client_instruction_next_steps(
    plans: list[ClientConfigPlan],
    *,
    blocked: bool,
    source_checkout: Path | None,
) -> list[str]:
    if blocked:
        return [
            "Resolve the conflicting client config entries, then re-run this MCP instruction tool.",
        ]
    return [
        f"Run `{_client_apply_command(plan.client, source_checkout=source_checkout)}` "
        f"to apply the {plan.client} client integration."
        for plan in plans
        if plan.action != "no_change"
    ] or ["No client config changes are needed."]


def _client_source_checkout_issue_with_command(
    issue: FirstRunIssue,
    *,
    command: str,
) -> FirstRunIssue:
    if issue.reason_code not in _SOURCE_CHECKOUT_REMEDIATION_REASON_CODES:
        return issue
    remediation = issue.remediation
    if not _is_source_checkout_remediation_command(remediation.related_command):
        return issue
    return issue.model_copy(
        update={"remediation": remediation.model_copy(update={"related_command": command})}
    )


def _client_source_checkout_blocked_payload_with_explicit_command(
    payload: FirstRunPayload,
    *,
    selected_clients: list[str],
    source_checkout: Path | None,
) -> FirstRunPayload:
    command = _client_instruction_command(selected_clients, source_checkout=source_checkout)
    update: dict[str, Any] = {
        "command": command,
        "next_steps": (f"Fix the reported --source-checkout path above, then re-run {command}.",),
    }
    if payload.issues:
        update["issues"] = tuple(
            _client_source_checkout_issue_with_command(issue, command=command)
            for issue in payload.issues
        )
    return payload.model_copy(
        update=update,
    )


def _client_env_file_missing_payload_with_explicit_command(
    payload: FirstRunPayload,
    *,
    selected_clients: list[str],
    source_checkout: Path | None,
) -> FirstRunPayload:
    command = _client_instruction_command(selected_clients, source_checkout=source_checkout)
    update: dict[str, Any] = {
        "command": command,
        "next_steps": _client_instruction_reason_coded_next_steps(
            payload.reason_code or SETUP_READINESS_FAILED,
            payload.next_steps,
            command=command,
        ),
    }
    if source_checkout is not None and payload.issues:
        update["issues"] = _start_issues_with_command(
            payload.issues,
            command=_start_source_checkout_command(source_checkout),
            source_checkout=source_checkout,
        )
    return payload.model_copy(update=update)


def _client_env_file_missing_source_checkout(
    source_checkout: Path | None,
    env_file: Path,
) -> Path | None:
    if source_checkout is not None:
        return source_checkout
    try:
        config = read_host_setup_config()
    except HostSetupConfigError:
        return None
    if config.source_checkout is None:
        return None
    persisted_root = _resolve_user_supplied_path(config.source_checkout.root)
    env_root = _resolve_user_supplied_path(env_file.parent)
    if persisted_root != env_root:
        return None
    return persisted_root


def _client_instruction_reason_coded_payload(
    reason_code: str,
    summary: str,
    details: dict[str, Any],
    *,
    requested_clients: list[str],
    selected_clients: list[str],
    source_checkout: Path | None,
) -> FirstRunPayload:
    command = _client_instruction_command(
        selected_clients or requested_clients,
        source_checkout=source_checkout,
    )
    payload = _reason_coded_payload(reason_code, summary, details)
    return payload.model_copy(
        update={
            "command": command,
            "next_steps": _client_instruction_reason_coded_next_steps(
                reason_code,
                payload.next_steps,
                command=command,
            ),
        }
    )


def _client_instruction_reason_coded_next_steps(
    reason_code: str,
    next_steps: tuple[str, ...],
    *,
    command: str,
) -> tuple[str, ...]:
    if reason_code == SETUP_CLIENT_UNKNOWN:
        return (
            f"Re-run {command} with a supported --client; the accepted names are "
            "listed under known_clients in the issue details.",
        )
    return tuple(
        _client_instruction_reason_coded_next_step(step, command=command) for step in next_steps
    )


def _client_instruction_reason_coded_next_step(step: str, *, command: str) -> str:
    return _START_REASON_CODED_SETUP_COMMAND_PATTERN.sub(lambda _: command, step, count=1)


def _client_instruction_command(clients: list[str], *, source_checkout: Path | None) -> str:
    command = ["awf", "setup"]
    for client in clients:
        command.extend(["--client", client])
    if source_checkout is not None:
        command.extend(["--source-checkout", str(source_checkout)])
    return shlex.join(command)


def _client_apply_command(client: str, *, source_checkout: Path | None) -> str:
    command = ["awf", "setup", "--client", client]
    if source_checkout is not None:
        command.extend(["--source-checkout", str(source_checkout)])
    return shlex.join(command)


def _resolve_project_init_path(project_path: str) -> Path:
    return _resolve_user_supplied_path(project_path)


def _resolve_client_source_checkout_path(source_checkout: str | None) -> Path | None:
    if source_checkout is None:
        return None

    return _resolve_user_supplied_path(source_checkout)


def _resolve_user_supplied_path(raw_path: str | Path) -> Path:
    candidate = Path(raw_path)
    try:
        expanded = candidate.expanduser()
    except (OSError, RuntimeError, ValueError):
        return candidate.absolute()

    try:
        return expanded.resolve()
    except (OSError, RuntimeError, ValueError):
        return expanded.absolute()


def _mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return cast(Mapping[str, Any], value)
    return {}


def _list_of_strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]
