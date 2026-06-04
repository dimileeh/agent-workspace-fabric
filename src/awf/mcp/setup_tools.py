"""MCP tools for AWF first-run setup/start/init/client flows."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Protocol, cast

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult
from pydantic import Field

from awf.api.schemas import ErrorResponse
from awf.cli.init_ops import (
    _existing_project_profile_path,
    _init_project_onboarding_payload,
)
from awf.cli.setup_commands import (
    _client_env,
    _client_home,
    _client_now,
    _client_which,
    _config_error_details,
    _reason_coded_payload,
    _resolve_client_env_file,
    _run_setup,
)
from awf.cli.start_commands import (
    _resolve_start_bootstrap_inputs,
    _resolve_start_source_checkout,
    _source_checkout_failure_payload,
    _start_failure_payload,
    _start_success_payload,
)
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
    FirstRunPayload,
    render_first_run_json,
)
from awf.host_setup.source_assets import SourceCheckoutError
from awf.host_setup.system_checks import SetupCheckError
from awf.profiles.onboarding import preview_project_onboarding, write_workspace_profile
from awf.service.bootstrap import (
    ServiceBootstrapError,
    ServiceBootstrapOptions,
    run_service_bootstrap,
)

StructuredToolResult = Annotated[CallToolResult, dict[str, Any]]

START_OPTIONS_INVALID = "START_OPTIONS_INVALID"
PROJECT_INIT_INVALID_PATH = "PROJECT_INIT_INVALID_PATH"
PROJECT_PROFILE_EXISTS = "PROJECT_PROFILE_EXISTS"
PROJECT_INIT_FAILED = "PROJECT_INIT_FAILED"


class SafeResult(Protocol):
    """Protocol for constructing redacted MCP tool result objects."""

    def __call__(self, payload: dict[str, Any], *, is_error: bool = False) -> CallToolResult:
        """Build a safe MCP tool result from a JSON payload."""
        ...


def register_setup_tools(
    mcp: FastMCP,
    safe_result: SafeResult,
    settings_value: Settings,
) -> None:
    """Register first-run setup/start/init/client MCP tools."""
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
        return _get_setup_status_result(
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
        return _initialize_project_profile_result(
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
        return _client_integration_instructions_result(
            safe_result=safe_result,
            clients=clients or list(CLIENT_DESCRIPTORS),
            source_checkout=source_checkout,
        )


def _get_setup_status_result(
    *,
    safe_result: SafeResult,
    providers: list[str],
    source_checkout: str | None,
) -> CallToolResult:
    source_path = Path(source_checkout).expanduser() if source_checkout is not None else None
    try:
        readiness = _run_setup(
            providers=providers,
            dry_run=True,
            non_interactive=True,
            allow_plain_secrets=False,
            source_checkout=source_path,
        )
        config = HostSetupConfig() if source_path is not None else read_host_setup_config()
    except SetupCheckError as exc:
        return _first_run_result(
            safe_result,
            _reason_coded_payload(exc.reason_code, str(exc), exc.details),
            is_error=True,
        )
    except HostSetupConfigError as exc:
        return _first_run_result(
            safe_result,
            _reason_coded_payload(exc.reason_code, exc.message, _config_error_details(exc)),
            is_error=True,
        )

    rendered = render_first_run_json(readiness)
    details = _mapping(rendered.get("details"))
    payload = {
        "status": rendered.get("status", "unknown"),
        "command": rendered.get("command", "awf setup"),
        "summary": rendered.get("summary", ""),
        "reason_code": rendered.get("reason_code"),
        "setup": {
            "dry_run": True,
            "selected_providers": _list_of_strings(details.get("selected_providers")),
            "checks": _safe_setup_checks(details.get("checks")),
            "plain_file_consent": config.consent.plain_file_secrets,
            "source_checkout_assets_consent": config.consent.source_checkout_assets,
            "config_path": str(default_host_setup_config_path()),
        },
        "providers": _provider_statuses(config.providers),
        "clients": _client_statuses(config.clients),
        "source_checkout": _source_checkout_status(config),
        "issues": _setup_status_issues(rendered.get("issues")),
        "next_steps": _list_of_strings(rendered.get("next_steps")),
    }
    return safe_result(payload, is_error=payload["status"] in ("blocked", "failed"))


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

    source_path = Path(source_checkout).expanduser() if source_checkout is not None else None
    try:
        verified = _resolve_start_source_checkout(source_path)
        inputs = _resolve_start_bootstrap_inputs(verified)
        options = ServiceBootstrapOptions(
            timeout_seconds=timeout_seconds,
            skip_agent_runtime_build=skip_agent_runtime_build,
            force_rebuild=rebuild,
        )
        result = await run_service_bootstrap(
            inputs.settings,
            options=options,
            compose_file=inputs.compose_file,
            env_file=inputs.compose_env_file,
            asset_root=inputs.asset_root,
            service_environ=inputs.service_env,
        )
    except SourceCheckoutError as exc:
        return _first_run_result(
            safe_result,
            _source_checkout_failure_payload(exc),
            is_error=True,
        )
    except ServiceBootstrapError as exc:
        return _first_run_result(safe_result, _start_failure_payload(exc), is_error=True)

    return _first_run_result(safe_result, _start_success_payload(inputs.settings, result))


def _initialize_project_profile_result(
    *,
    safe_result: SafeResult,
    project_path: str,
    include_smoke_request: bool,
    write_profile: bool,
    template: str,
    force: bool,
) -> CallToolResult:
    repository = Path(project_path).expanduser().resolve()
    if not repository.exists():
        return _project_init_path_error(
            safe_result,
            "project path does not exist",
            repository=repository,
        )
    if not repository.is_dir():
        return _project_init_path_error(
            safe_result,
            "project path is not a directory",
            repository=repository,
        )

    existing_profile_path = _existing_project_profile_path(repository)
    try:
        preview = preview_project_onboarding(
            repository,
            template=template,
            include_smoke_request=include_smoke_request,
        )
    except ValueError as exc:
        return _project_init_path_error(safe_result, str(exc), repository=repository)
    except Exception as exc:
        return _error_result(
            safe_result,
            PROJECT_INIT_FAILED,
            f"could not build onboarding preview: {exc}",
            detail={"project_path": str(repository), "template": template},
        )

    written_path: Path | None = None
    if write_profile:
        try:
            written_path = write_workspace_profile(preview, force=force)
        except FileExistsError as exc:
            return _error_result(
                safe_result,
                PROJECT_PROFILE_EXISTS,
                str(exc),
                detail={"project_path": str(repository), "force": force},
            )
        except OSError as exc:
            return _error_result(
                safe_result,
                PROJECT_INIT_FAILED,
                f"could not write project profile: {type(exc).__name__}",
                detail={"project_path": str(repository), "force": force},
            )

    mode = "write" if write_profile else "preview"
    payload = _init_project_onboarding_payload(
        preview=preview,
        existing_profile_path=existing_profile_path,
        written_path=written_path,
        service_status={"service": "awf", "status": "not_checked", "source": "mcp"},
        doctor_status="not_checked",
        local_checks_ready=False,
        guided=False,
        mode=mode,
    )
    return safe_result(cast(dict[str, Any], payload))


def _client_integration_instructions_result(
    *,
    safe_result: SafeResult,
    clients: list[str],
    source_checkout: str | None,
) -> CallToolResult:
    source_path = Path(source_checkout).expanduser() if source_checkout is not None else None
    try:
        selected = normalize_clients(clients)
        env_file = _resolve_client_env_file(source_path, False)
    except SetupCheckError as exc:
        return _first_run_result(
            safe_result,
            _reason_coded_payload(exc.reason_code, str(exc), exc.details),
            is_error=True,
        )
    except SourceCheckoutError as exc:
        from awf.cli.setup_commands import _client_source_checkout_blocked_payload

        return _first_run_result(
            safe_result,
            _client_source_checkout_blocked_payload(exc),
            is_error=True,
        )

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
    blocked = [plan for plan in plans if plan.action == "conflict"]
    status = "blocked" if blocked else "success"
    payload: dict[str, Any] = {
        "status": status,
        "command": "awf setup --client",
        "summary": _client_instructions_summary(plans, blocked=bool(blocked)),
        "env_file": str(env_file),
        "clients": [_client_instruction_payload(plan) for plan in plans],
        "next_steps": _client_instruction_next_steps(plans, blocked=bool(blocked)),
    }
    if blocked:
        payload["reason_code"] = CLIENT_CONFIG_CONFLICT
    return safe_result(payload, is_error=bool(blocked))


def _first_run_result(
    safe_result: SafeResult,
    payload: FirstRunPayload,
    *,
    is_error: bool = False,
) -> CallToolResult:
    return safe_result(render_first_run_json(payload), is_error=is_error)


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
    *,
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


def _client_instruction_payload(plan: ClientConfigPlan) -> dict[str, Any]:
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
        "apply_command": f"awf setup --client {plan.client}",
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
) -> list[str]:
    if blocked:
        return [
            "Resolve the conflicting client config entries, then re-run this MCP instruction tool.",
        ]
    return [
        f"Run `awf setup --client {plan.client}` to apply the {plan.client} client integration."
        for plan in plans
        if plan.action != "no_change"
    ] or ["No client config changes are needed."]


def _mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return cast(Mapping[str, Any], value)
    return {}


def _list_of_strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]
