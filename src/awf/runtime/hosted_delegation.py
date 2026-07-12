"""Hosted delegation contract and configuration helpers.

AWF Core delegates hosted PR-monitor repair and validation through an
authenticated asynchronous HTTP operation protocol. Core remains authoritative
for PR review state, CI state, waits, merge decisions, and audit events; the
hosting control plane only runs short-lived repair/validation jobs against the
existing PR branch.

Operation state machine for AWF Cloud implementers:

1. Core starts an operation with ``POST {base_url}/v1/agent-runs`` or
   ``POST {base_url}/v1/validation-runs``.
2. The request uses ``Authorization: Bearer <configured token>``. The token is
   never sent in the body or query string.
3. The start response returns ``operation_id``, ``workspace_id``, and an
   ``operation_url`` path or same-origin URL.
4. Core polls ``operation_url`` until a terminal state: ``succeeded``,
   ``failed``, ``cancelled``, or ``timed_out``. Every poll response must echo
   the same ``workspace_id`` and ``operation_id``.
5. Successful agent repair terminal responses must include ``returncode``,
   ``stdout``, ``stderr``, and ``terminal_head_sha`` for the remote PR head
   pushed by the host. Core fetches the PR branch and verifies that SHA before
   monitor bookkeeping continues. Failed, cancelled, and timed-out hosted
   operations are mapped from the operation state and must not be upgraded by
   stale success fields.
6. Validation terminal responses return Core-compatible validation command
   results; CI remains a separate required merge gate.

Bodies are secret-free. Prompt text is sent only in the request body field
reserved for stdin, never argv, query strings, or logs. Provider credentials
and delegation bearer tokens are never serialized into hosted requests.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from awf.adapters.runtime_executor import (
    _HOSTED_TIMEOUT_RETURN_CODE,
    AgentRuntimeExecRequest,
    AgentRuntimeExecResult,
    AgentRuntimeExecutor,
)
from awf.common.commands import COMMAND_TIMEOUT_REASON
from awf.common.config import Settings
from awf.common.logging import get_logger
from awf.common.token_patterns import TOKEN_ASSIGNMENT_KEY_PATTERN, compile_known_token_re
from awf.profiles.models import WorkspaceProfile
from awf.runtime.validation_setup import (
    PROFILE_PREFLIGHT_PHASE,
    PROFILE_VALIDATION_TOOL_UNAVAILABLE,
    profile_phase_command_plan,
    profile_validation_tool_preflight_findings,
)
from awf.runtime.validation_types import (
    ValidateCommandProbeTarget,
    ValidateToolProbeResult,
    ValidationCommandResult,
    ValidationCoverageResult,
    ValidationResult,
)

HOSTED_DELEGATION_MISSING_BASE_URL = "AWF_HOSTED_DELEGATION_BASE_URL"
HOSTED_DELEGATION_MISSING_TOKEN = (
    "AWF_HOSTED_DELEGATION_BEARER_TOKEN or AWF_HOSTED_DELEGATION_BEARER_TOKEN_ENV"
)
_ARTIFACT_LABEL_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9_-]+")
_ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ENV_REFERENCE_PATTERN = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")
_SECRET_ENV_NAME_PATTERN = re.compile(
    rf"^(?:{TOKEN_ASSIGNMENT_KEY_PATTERN})$|"
    r"(?:^|[_-])(?:TOKEN|API[_-]?KEY|ACCESS[_-]?KEY|PRIVATE[_-]?KEY|"
    r"PASSWORD|PASSWD|SECRET|CREDENTIALS?)(?:[_-]|$)",
    re.IGNORECASE,
)
_SECRET_VALUE_PATTERN = compile_known_token_re(match_truncated_provider_tokens=False)
_URL_WITH_CREDENTIALS_PATTERN = re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]*://[^/?#\s@]+@")
_HOSTED_RESPONSE_JSON_OVERHEAD_BYTES = 64 * 1024
_HOSTED_VALIDATION_TERMINAL_FAILURES = {
    "failed": (1, "HOSTED_VALIDATION_FAILED"),
    "cancelled": (130, "HOSTED_VALIDATION_CANCELLED"),
    "timed_out": (_HOSTED_TIMEOUT_RETURN_CODE, "HOSTED_VALIDATION_TIMED_OUT"),
}
_HOSTED_AGENT_TERMINAL_FAILURES = {
    "failed": (1, ""),
    "cancelled": (130, ""),
    "timed_out": (_HOSTED_TIMEOUT_RETURN_CODE, COMMAND_TIMEOUT_REASON),
}
_HOSTED_PR_IDENTITY_URL_FIELDS = frozenset({"repo_url", "head_repo_url"})
_HOSTED_COVERAGE_OMITTED_RUNTIME_ENV = frozenset({"PIP_EXTRA_INDEX_URL", "PIP_INDEX_URL"})
_log = get_logger(__name__)


class HostedDelegationConfigError(ValueError):
    """Raised when hosted mode is requested without complete delegation settings."""

    def __init__(self, *, missing: tuple[str, ...]) -> None:
        self.missing = missing
        super().__init__("Hosted delegation is not configured.")

    def detail(self) -> dict[str, list[str]]:
        return {"missing": list(self.missing)}


class HostedDelegationProtocolError(RuntimeError):
    """Raised when the host returns a malformed or cross-workspace operation."""


@dataclass(frozen=True, slots=True)
class HostedDelegationConfig:
    """Resolved hosted delegation settings with secret values kept in memory only."""

    base_url: str
    bearer_token: str
    poll_interval_seconds: float
    operation_timeout_seconds: float
    request_timeout_seconds: float
    cancel_timeout_seconds: float
    max_output_bytes: int

    def redacted_payload(self) -> dict[str, Any]:
        """Return a secret-free diagnostic/config projection."""

        return {
            "base_url": self.base_url,
            "bearer_token": "<redacted>",
            "poll_interval_seconds": self.poll_interval_seconds,
            "operation_timeout_seconds": self.operation_timeout_seconds,
            "request_timeout_seconds": self.request_timeout_seconds,
            "cancel_timeout_seconds": self.cancel_timeout_seconds,
            "max_output_bytes": self.max_output_bytes,
        }


def hosted_delegation_config_from_settings(
    settings: Settings,
    *,
    environ: Mapping[str, str] | None = None,
) -> HostedDelegationConfig:
    """Resolve hosted delegation config or raise a redacted diagnostic error."""

    return hosted_delegation_config_from_values(
        base_url=settings.hosted_delegation_base_url,
        bearer_token=settings.hosted_delegation_bearer_token,
        bearer_token_env=settings.hosted_delegation_bearer_token_env,
        environ=environ,
        poll_interval_seconds=settings.hosted_delegation_poll_interval_seconds,
        operation_timeout_seconds=settings.hosted_delegation_operation_timeout_seconds,
        request_timeout_seconds=settings.hosted_delegation_request_timeout_seconds,
        cancel_timeout_seconds=settings.hosted_delegation_cancel_timeout_seconds,
        max_output_bytes=settings.hosted_delegation_max_output_bytes,
    )


def hosted_delegation_config_from_values(
    *,
    base_url: str | None,
    bearer_token: str | None,
    bearer_token_env: str | None = None,
    environ: Mapping[str, str] | None = None,
    poll_interval_seconds: float,
    operation_timeout_seconds: float,
    request_timeout_seconds: float,
    cancel_timeout_seconds: float,
    max_output_bytes: int,
) -> HostedDelegationConfig:
    """Resolve hosted delegation config from already-selected settings values."""

    env = os.environ if environ is None else environ
    missing: list[str] = []
    resolved_base_url = _normalized_url(base_url)
    if resolved_base_url is None:
        missing.append(HOSTED_DELEGATION_MISSING_BASE_URL)
    token = _normalized_secret(bearer_token)
    token_env = _normalized_env_name(bearer_token_env)
    if token is None and token_env is not None:
        token = _normalized_secret(env.get(token_env))
    if token is None:
        missing.append(HOSTED_DELEGATION_MISSING_TOKEN)
    if missing:
        raise HostedDelegationConfigError(missing=tuple(missing))
    assert resolved_base_url is not None
    assert token is not None
    return HostedDelegationConfig(
        base_url=resolved_base_url,
        bearer_token=token,
        poll_interval_seconds=poll_interval_seconds,
        operation_timeout_seconds=operation_timeout_seconds,
        request_timeout_seconds=request_timeout_seconds,
        cancel_timeout_seconds=cancel_timeout_seconds,
        max_output_bytes=max_output_bytes,
    )


class HostedAgentRuntimeExecutor(AgentRuntimeExecutor):
    """HTTP-backed hosted agent runtime executor for explicit hosted adoption."""

    def __init__(
        self,
        config: HostedDelegationConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._client = client

    async def execute(self, request: AgentRuntimeExecRequest) -> AgentRuntimeExecResult:
        """Start a hosted agent operation, poll it, and return the terminal result."""

        operation: _HostedOperationRef | None = None
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self._config.request_timeout_seconds)
        try:
            operation = await self._start(client, request)
            terminal = await self._poll_operation(
                client,
                operation,
                terminal_head_sha_required=True,
            )
        except asyncio.CancelledError:
            if operation is not None:
                await self._cancel_operation(client, operation)
            raise
        except TimeoutError:
            if operation is not None:
                await self._cancel_operation(client, operation)
            return AgentRuntimeExecResult(
                returncode=_HOSTED_TIMEOUT_RETURN_CODE,
                stdout="",
                stderr=(
                    "hosted delegation operation timed out after "
                    f"{self._config.operation_timeout_seconds:g}s"
                ),
                timeout_reason=COMMAND_TIMEOUT_REASON,
            )
        finally:
            if owns_client:
                await client.aclose()
        return _agent_result_from_terminal(
            terminal,
            max_output_bytes=self._config.max_output_bytes,
        )

    async def _start(
        self,
        client: httpx.AsyncClient,
        request: AgentRuntimeExecRequest,
    ) -> _HostedOperationRef:
        response = await client.post(
            _join_url(self._config.base_url, "/v1/agent-runs"),
            headers=_auth_headers(self._config),
            json=_agent_start_payload(request),
            timeout=self._config.request_timeout_seconds,
        )
        response.raise_for_status()
        payload = _response_json(response)
        return _operation_ref_from_payload(
            payload,
            expected_workspace_id=request.workspace_id,
            base_url=self._config.base_url,
        )

    async def _poll_operation(
        self,
        client: httpx.AsyncClient,
        operation: _HostedOperationRef,
        *,
        terminal_head_sha_required: bool,
    ) -> Mapping[str, Any]:
        deadline = time.monotonic() + self._config.operation_timeout_seconds
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError
            payload = await _poll_response_json(
                client,
                operation.url,
                config=self._config,
            )
            _validate_operation_identity(payload, operation)
            state = _operation_state(payload)
            if state in {"queued", "running"}:
                await asyncio.sleep(self._config.poll_interval_seconds)
                continue
            if state in {"succeeded", "failed", "cancelled", "timed_out"}:
                if (
                    state == "succeeded"
                    and terminal_head_sha_required
                    and not _valid_sha(payload.get("terminal_head_sha"))
                ):
                    raise HostedDelegationProtocolError(
                        "hosted delegation terminal response missing terminal_head_sha"
                    )
                return payload
            raise HostedDelegationProtocolError("hosted delegation returned unknown state")

    async def _cancel_operation(
        self,
        client: httpx.AsyncClient,
        operation: _HostedOperationRef,
    ) -> None:
        await _cancel_operation(client, self._config, operation)


class HostedValidationDelegate:
    """HTTP-backed validation runner for explicit hosted PR monitor adoption."""

    def __init__(
        self,
        config: HostedDelegationConfig,
        *,
        artifacts_dir: Path,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._artifacts_dir = artifacts_dir
        self._client = client

    async def run_profile_tool_preflight(
        self,
        *,
        workspace_id: str,
        profile: WorkspaceProfile,
    ) -> ValidationResult:
        """Run local static validation-tool preflight before hosted validation."""
        findings = profile_validation_tool_preflight_findings(profile)
        if not findings:
            return ValidationResult()

        started = time.monotonic()
        workspace_artifacts = self._artifacts_dir / workspace_id
        workspace_artifacts.mkdir(parents=True, exist_ok=True)
        label = "01_profile_preflight"
        base_stream_id = f"validation.{label}"
        stdout_path = workspace_artifacts / f"{label}.stdout"
        stderr_path = workspace_artifacts / f"{label}.stderr"
        metadata: dict[str, object] = {"findings": [finding.as_metadata() for finding in findings]}
        stderr = json.dumps(metadata, sort_keys=True, indent=2) + "\n"
        await asyncio.to_thread(stdout_path.write_text, "", encoding="utf-8")
        await asyncio.to_thread(stderr_path.write_text, stderr, encoding="utf-8")

        _log.info(
            "hosted_validation.profile_tool_preflight_failed",
            workspace_id=workspace_id,
            reason_code=PROFILE_VALIDATION_TOOL_UNAVAILABLE,
            finding_count=len(findings),
        )
        result = ValidationCommandResult(
            command="profile validation tool preflight",
            returncode=1,
            duration_seconds=time.monotonic() - started,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            phase=PROFILE_PREFLIGHT_PHASE,
            reason_code=PROFILE_VALIDATION_TOOL_UNAVAILABLE,
            stream_ids={
                "stdout": f"{base_stream_id}.stdout",
                "stderr": f"{base_stream_id}.stderr",
            },
            policy_failed=True,
            metadata=metadata,
        )
        return ValidationResult(commands=[result])

    async def probe_validate_command_tools(
        self,
        *,
        workspace_id: str,
        compose_project: str,
        compose_file: Path,
        profile: WorkspaceProfile,
    ) -> ValidateToolProbeResult:
        """Delegate the post-setup validate-command toolchain probe to the host."""
        del compose_project, compose_file
        terminal = await self._run_operation(
            workspace_id=workspace_id,
            start_path="/v1/validation-runs",
            payload={
                "workspace_id": workspace_id,
                "profile": _hosted_validation_profile_payload(profile),
                "phase_names": [],
                "run_healthchecks": False,
                "include_coverage": False,
                "probe": "validate_toolchain",
            },
            poll_response_max_bytes=_response_json_max_bytes(self._config.max_output_bytes),
        )
        return _validate_tool_probe_result_from_terminal(terminal)

    async def run_profile_phases(
        self,
        *,
        workspace_id: str,
        compose_project: str,
        compose_file: Path,
        profile: WorkspaceProfile,
        phase_names: list[str] | tuple[str, ...],
        run_healthchecks: bool = False,
        worktree_path: Path | None = None,
        include_coverage: bool = True,
        pr_identity: Mapping[str, Any] | None = None,
    ) -> ValidationResult:
        """Delegate selected profile phases to the hosting control plane."""

        del compose_project, compose_file
        expected_command_count = _hosted_validation_expected_command_count(
            profile,
            phase_names,
            run_healthchecks=run_healthchecks,
        )
        terminal = await self._run_operation(
            workspace_id=workspace_id,
            start_path="/v1/validation-runs",
            payload={
                "workspace_id": workspace_id,
                "profile": _hosted_validation_profile_payload(profile),
                "phase_names": list(phase_names),
                "run_healthchecks": run_healthchecks,
                "worktree_path": str(worktree_path) if worktree_path is not None else None,
                "include_coverage": include_coverage,
                "pr_identity": _hosted_pr_identity_payload(pr_identity or {}),
            },
            poll_response_max_bytes=_response_json_max_bytes(
                self._config.max_output_bytes,
                output_slots=_hosted_validation_poll_output_slots(
                    profile,
                    phase_names,
                    include_coverage=include_coverage,
                    expected_command_count=expected_command_count,
                ),
            ),
        )
        return _validation_result_from_terminal(
            terminal,
            artifacts_dir=self._artifacts_dir / workspace_id,
            max_output_bytes=self._config.max_output_bytes,
            expected_command_count=expected_command_count,
        )

    async def run_profile_coverage(
        self,
        *,
        workspace_id: str,
        compose_project: str,
        compose_file: Path,
        profile: WorkspaceProfile,
        phase: str = "coverage",
        parallel_worker_cpu_limit: int | None = None,
        pr_identity: Mapping[str, Any] | None = None,
    ) -> ValidationCoverageResult | None:
        """Delegate a hosted coverage-only operation."""

        del compose_project, compose_file
        terminal = await self._run_operation(
            workspace_id=workspace_id,
            start_path="/v1/validation-runs",
            payload={
                "workspace_id": workspace_id,
                "profile": _hosted_validation_profile_payload(
                    profile,
                    omit_runtime_environment=_HOSTED_COVERAGE_OMITTED_RUNTIME_ENV,
                ),
                "phase_names": [phase],
                "run_healthchecks": False,
                "include_coverage": True,
                "parallel_worker_cpu_limit": parallel_worker_cpu_limit,
                "pr_identity": _hosted_pr_identity_payload(pr_identity or {}),
            },
            poll_response_max_bytes=_response_json_max_bytes(
                self._config.max_output_bytes,
                output_slots=1,
            ),
        )
        state = _operation_state(terminal)
        if state in _HOSTED_VALIDATION_TERMINAL_FAILURES:
            return _coverage_terminal_failure_result(
                terminal,
                artifacts_dir=self._artifacts_dir / workspace_id,
                max_output_bytes=self._config.max_output_bytes,
            )
        coverage = terminal.get("coverage")
        if coverage is None:
            raise HostedDelegationProtocolError(
                "hosted validation terminal response missing coverage"
            )
        if not isinstance(coverage, Mapping):
            raise HostedDelegationProtocolError(
                "hosted validation terminal response has malformed coverage"
            )
        return _coverage_result_from_payload(
            coverage,
            artifacts_dir=self._artifacts_dir / workspace_id,
            max_output_bytes=self._config.max_output_bytes,
        )

    async def _run_operation(
        self,
        *,
        workspace_id: str,
        start_path: str,
        payload: Mapping[str, Any],
        poll_response_max_bytes: int,
    ) -> Mapping[str, Any]:
        operation: _HostedOperationRef | None = None
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self._config.request_timeout_seconds)
        try:
            operation = await self._start_operation(
                client,
                workspace_id=workspace_id,
                start_path=start_path,
                payload=payload,
            )
            return await self._poll_operation(
                client,
                operation,
                poll_response_max_bytes=poll_response_max_bytes,
            )
        except asyncio.CancelledError:
            if operation is not None:
                await _cancel_operation(client, self._config, operation)
            raise
        except TimeoutError as exc:
            if operation is not None:
                await _cancel_operation(client, self._config, operation)
            raise HostedDelegationProtocolError("hosted validation operation timed out") from exc
        finally:
            if owns_client:
                await client.aclose()

    async def _start_operation(
        self,
        client: httpx.AsyncClient,
        *,
        workspace_id: str,
        start_path: str,
        payload: Mapping[str, Any],
    ) -> _HostedOperationRef:
        response = await client.post(
            _join_url(self._config.base_url, start_path),
            headers=_auth_headers(self._config),
            json=dict(payload),
            timeout=self._config.request_timeout_seconds,
        )
        response.raise_for_status()
        return _operation_ref_from_payload(
            _response_json(response),
            expected_workspace_id=workspace_id,
            base_url=self._config.base_url,
        )

    async def _poll_operation(
        self,
        client: httpx.AsyncClient,
        operation: _HostedOperationRef,
        *,
        poll_response_max_bytes: int,
    ) -> Mapping[str, Any]:
        deadline = time.monotonic() + self._config.operation_timeout_seconds
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError
            payload = await _poll_response_json(
                client,
                operation.url,
                config=self._config,
                max_bytes=poll_response_max_bytes,
            )
            _validate_operation_identity(payload, operation)
            state = _operation_state(payload)
            if state in {"queued", "running"}:
                await asyncio.sleep(self._config.poll_interval_seconds)
                continue
            if state in {"succeeded", "failed", "cancelled", "timed_out"}:
                return payload
            raise HostedDelegationProtocolError("hosted delegation returned unknown state")


@dataclass(frozen=True, slots=True)
class _HostedOperationRef:
    operation_id: str
    workspace_id: str | None
    url: str


def _auth_headers(config: HostedDelegationConfig) -> dict[str, str]:
    return {"Authorization": f"Bearer {config.bearer_token}"}


def _agent_start_payload(request: AgentRuntimeExecRequest) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "workspace_id": request.workspace_id,
        "agent_runtime": request.agent_runtime.value,
        "cli_args": list(request.cli_args),
        "prompt_stdin_base64": base64.b64encode(request.prompt_stdin).decode("ascii"),
        "log_source": request.log_source,
        "model": request.model,
        "effort": request.effort,
        "env_passthrough_names": list(request.env_passthrough_names),
        "env_passthrough_aliases": [
            {"target": target, "source": source}
            for target, source in request.env_passthrough_aliases
        ],
        "file_auth_mount_targets": list(request.file_auth_mount_targets),
        "profile_env": [{"name": name, "value": value} for name, value in request.profile_env],
        "timeouts": {
            "wall_seconds": request.wall_timeout_seconds,
            "idle_seconds": request.idle_timeout_seconds,
        },
    }
    pr_identity = _agent_pr_identity_payload(request)
    if pr_identity:
        payload["pr_identity"] = pr_identity
    return payload


def _agent_pr_identity_payload(request: AgentRuntimeExecRequest) -> dict[str, Any]:
    identity: dict[str, Any] = {}
    for key in (
        "repo_url",
        "pr_url",
        "pr_number",
        "base_ref",
        "head_ref",
        "head_repo_url",
        "head_repo_slug",
        "expected_head_sha",
    ):
        value = getattr(request, key)
        if value is not None:
            if key in _HOSTED_PR_IDENTITY_URL_FIELDS and isinstance(value, str):
                value = _strip_url_userinfo(value)
            identity[key] = value
    if request.owned_paths:
        identity["owned_paths"] = list(request.owned_paths)
    return identity


def _hosted_pr_identity_payload(identity: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(identity)
    for key in _HOSTED_PR_IDENTITY_URL_FIELDS:
        value = payload.get(key)
        if isinstance(value, str):
            payload[key] = _strip_url_userinfo(value)
    return payload


def _strip_url_userinfo(value: str) -> str:
    parsed = urlsplit(value)
    if not parsed.scheme or "@" not in parsed.netloc:
        return value
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc.rsplit("@", 1)[1],
            parsed.path,
            parsed.query,
            parsed.fragment,
        )
    )


def _hosted_validation_profile_payload(
    profile: WorkspaceProfile,
    *,
    omit_runtime_environment: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    payload = profile.model_dump(mode="json", by_alias=True)
    _hosted_validation_sanitize_secret_refs(payload.get("secrets"))
    if omit_runtime_environment:
        _hosted_validation_omit_environment_entries(
            payload.get("runtime"),
            names=omit_runtime_environment,
        )
    _hosted_validation_sanitize_environment_container(payload.get("runtime"))
    services = payload.get("services")
    if isinstance(services, list):
        for service in services:
            _hosted_validation_sanitize_environment_container(service)
    return payload


def _hosted_validation_sanitize_secret_refs(secrets: object) -> None:
    if not isinstance(secrets, list):
        return
    for secret in secrets:
        if isinstance(secret, dict) and not _hosted_validation_preserves_secret_ref(secret):
            secret.pop("ref", None)


def _hosted_validation_preserves_secret_ref(secret: Mapping[str, object]) -> bool:
    kind = secret.get("kind")
    provider = secret.get("provider")
    ref = secret.get("ref")
    if kind != "env" or not isinstance(provider, str):
        return False
    if provider.strip().lower() != "env":
        return False
    return _hosted_validation_env_secret_ref_name(ref) is not None


def _hosted_validation_env_secret_ref_name(ref: object) -> str | None:
    if not isinstance(ref, str):
        return None
    stripped = ref.strip()
    if stripped.startswith("env/"):
        stripped = stripped[len("env/") :]
    if not _ENV_NAME_PATTERN.fullmatch(stripped):
        return None
    return stripped


def _hosted_validation_omit_environment_entries(
    container: object, *, names: frozenset[str]
) -> None:
    if not isinstance(container, dict):
        return
    environment = container.get("environment")
    if not isinstance(environment, dict):
        return
    for name in names:
        environment.pop(name, None)


def _hosted_validation_sanitize_environment_container(container: object) -> None:
    if not isinstance(container, dict):
        return
    environment = container.get("environment")
    if not isinstance(environment, dict):
        return
    container["environment"] = {
        str(name): _hosted_validation_env_value(str(name), value)
        for name, value in environment.items()
    }


def _hosted_validation_env_value(name: str, value: object) -> str:
    text = str(value)
    if _hosted_validation_env_value_is_secret(name, text):
        return f"${{{name}}}" if _ENV_NAME_PATTERN.fullmatch(name) else "<redacted>"
    return text


def _hosted_validation_env_value_is_secret(name: str, value: str) -> bool:
    stripped = value.strip()
    if not stripped or _ENV_REFERENCE_PATTERN.fullmatch(stripped):
        return False
    return (
        bool(_SECRET_ENV_NAME_PATTERN.search(name))
        or bool(_SECRET_VALUE_PATTERN.search(stripped))
        or bool(_URL_WITH_CREDENTIALS_PATTERN.search(stripped))
        or "-----BEGIN " in stripped
        or "\n" in stripped
    )


def _hosted_validation_poll_output_slots(
    profile: WorkspaceProfile,
    phase_names: list[str] | tuple[str, ...],
    *,
    include_coverage: bool,
    expected_command_count: int,
) -> int:
    requested_phases = set(phase_names)
    output_slots = expected_command_count
    if (
        include_coverage
        and "validate" in requested_phases
        and profile.validation.coverage.command is not None
    ):
        output_slots += 1
    # Failed operations may carry top-level stdout/stderr that becomes a synthetic result.
    return max(1, output_slots + 1)


def _hosted_validation_expected_command_count(
    profile: WorkspaceProfile,
    phase_names: list[str] | tuple[str, ...],
    *,
    run_healthchecks: bool,
) -> int:
    requested_phases = set(phase_names)
    return (
        len(profile_phase_command_plan(profile, phase_names))
        + int(run_healthchecks) * len(profile.validation.healthchecks)
        + int("validate" in requested_phases) * int(profile.validation.alembic.enabled)
    )


async def _poll_response_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    config: HostedDelegationConfig,
    max_bytes: int | None = None,
) -> Mapping[str, Any]:
    async with client.stream(
        "GET",
        url,
        headers=_auth_headers(config),
        timeout=config.request_timeout_seconds,
    ) as response:
        response.raise_for_status()
        return await _response_json_bounded(
            response,
            max_bytes=max_bytes
            if max_bytes is not None
            else _response_json_max_bytes(config.max_output_bytes),
        )


def _agent_result_from_terminal(
    payload: Mapping[str, Any],
    *,
    max_output_bytes: int,
) -> AgentRuntimeExecResult:
    state = _operation_state(payload)
    if state in _HOSTED_AGENT_TERMINAL_FAILURES:
        return _agent_terminal_failure_result(payload, max_output_bytes=max_output_bytes)
    stdout = _text_field(payload, "stdout")
    stderr = _text_field(payload, "stderr")
    _ensure_output_within_limit(stdout, stderr, max_output_bytes=max_output_bytes)
    return AgentRuntimeExecResult(
        returncode=_int_field(payload, "returncode"),
        stdout=stdout,
        stderr=stderr,
        timeout_reason=_optional_str(payload.get("timeout_reason")) or "",
        terminal_head_sha=_optional_str(payload.get("terminal_head_sha")),
    )


def _agent_terminal_failure_result(
    payload: Mapping[str, Any],
    *,
    max_output_bytes: int,
) -> AgentRuntimeExecResult:
    state = _operation_state(payload)
    returncode, default_timeout_reason = _HOSTED_AGENT_TERMINAL_FAILURES[state]
    stdout = _text_payload_field(payload, "stdout")
    stderr = _text_payload_field(payload, "stderr")
    if not stderr:
        message = _optional_str(payload.get("message"))
        stderr = f"{message or f'hosted agent operation {state}'}\n"
    _ensure_output_within_limit(stdout, stderr, max_output_bytes=max_output_bytes)
    return AgentRuntimeExecResult(
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        timeout_reason=_optional_str(payload.get("timeout_reason")) or default_timeout_reason,
        terminal_head_sha=None,
    )


def _validation_result_from_terminal(
    payload: Mapping[str, Any],
    *,
    artifacts_dir: Path,
    max_output_bytes: int,
    expected_command_count: int,
) -> ValidationResult:
    state = _operation_state(payload)
    if "commands" not in payload:
        if state in _HOSTED_VALIDATION_TERMINAL_FAILURES:
            commands_payload: object = []
        else:
            raise HostedDelegationProtocolError("hosted validation response missing commands")
    else:
        commands_payload = payload["commands"]
    if not isinstance(commands_payload, list):
        raise HostedDelegationProtocolError("hosted validation response has malformed commands")
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    commands = [
        _validation_command_result_from_payload(
            item,
            artifacts_dir=artifacts_dir,
            index=index,
            max_output_bytes=max_output_bytes,
        )
        for index, item in enumerate(commands_payload, start=1)
    ]
    if (
        state == "succeeded"
        and expected_command_count > 0
        and len(commands) < expected_command_count
        and not any(command.blocks_validation for command in commands)
    ):
        raise HostedDelegationProtocolError(
            "hosted validation terminal response missing command evidence"
        )
    if state in _HOSTED_VALIDATION_TERMINAL_FAILURES and not any(
        command.blocks_validation for command in commands
    ):
        commands.append(
            _validation_terminal_failure_result(
                payload,
                artifacts_dir=artifacts_dir,
                index=len(commands) + 1,
                max_output_bytes=max_output_bytes,
            )
        )
    coverage_payload = payload.get("coverage")
    coverage = (
        _coverage_result_from_payload(
            coverage_payload,
            artifacts_dir=artifacts_dir,
            max_output_bytes=max_output_bytes,
        )
        if isinstance(coverage_payload, Mapping)
        else None
    )
    return ValidationResult(commands=commands, coverage=coverage)


def _validation_terminal_failure_result(
    payload: Mapping[str, Any],
    *,
    artifacts_dir: Path,
    index: int,
    max_output_bytes: int,
    default_command: str = "hosted validation operation",
    default_phase: str = "validate",
) -> ValidationCommandResult:
    state = _operation_state(payload)
    returncode, reason_code = _HOSTED_VALIDATION_TERMINAL_FAILURES[state]
    stdout = _text_payload_field(payload, "stdout")
    stderr = _text_payload_field(payload, "stderr")
    if not stderr:
        message = _optional_str(payload.get("message"))
        stderr = f"{message or f'{default_command} {state}'}\n"
    return _validation_command_result_from_payload(
        {
            "command": _optional_str(payload.get("command")) or default_command,
            "returncode": returncode,
            "duration_seconds": _optional_float(payload.get("duration_seconds")) or 0.0,
            "stdout": stdout,
            "stderr": stderr,
            "phase": _optional_str(payload.get("phase")) or default_phase,
            "reason_code": _optional_str(payload.get("reason_code")) or reason_code,
            "metadata": {
                "hosted_operation_state": state,
            },
        },
        artifacts_dir=artifacts_dir,
        index=index,
        max_output_bytes=max_output_bytes,
    )


def _coverage_terminal_failure_result(
    payload: Mapping[str, Any],
    *,
    artifacts_dir: Path,
    max_output_bytes: int,
) -> ValidationCoverageResult:
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    command_result = _validation_terminal_failure_result(
        payload,
        artifacts_dir=artifacts_dir,
        index=999,
        max_output_bytes=max_output_bytes,
        default_command="hosted coverage operation",
        default_phase="coverage",
    )
    return ValidationCoverageResult(
        provider="hosted",
        percent=None,
        minimum_percent=0.0,
        enforce=True,
        status="failed",
        reason_code=command_result.reason_code,
        command_result=command_result,
    )


def _validation_command_result_from_payload(
    payload: object,
    *,
    artifacts_dir: Path,
    index: int,
    max_output_bytes: int,
) -> ValidationCommandResult:
    if not isinstance(payload, Mapping):
        raise HostedDelegationProtocolError("hosted validation command result is malformed")
    phase = _optional_str(payload.get("phase")) or "validate"
    label = f"{index:02d}_{_artifact_label_component(phase)}"
    stdout = _text_payload_field(payload, "stdout")
    stderr = _text_payload_field(payload, "stderr")
    _ensure_output_within_limit(stdout, stderr, max_output_bytes=max_output_bytes)
    stdout_path = artifacts_dir / f"{label}.stdout"
    stderr_path = artifacts_dir / f"{label}.stderr"
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")
    stream_ids = payload.get("stream_ids", {})
    if not isinstance(stream_ids, dict):
        stream_ids = {}
    metadata = payload.get("metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}
    return ValidationCommandResult(
        command=_str_field(payload, "command"),
        returncode=_int_field(payload, "returncode"),
        duration_seconds=_float_field(payload, "duration_seconds"),
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        phase=phase,
        reason_code=_optional_str(payload.get("reason_code")) or "COMMAND_FAILED",
        stream_ids={str(key): _optional_str(value) for key, value in stream_ids.items()},
        retry_count=_int_payload_field(payload.get("retry_count"), default=0),
        policy_failed=bool(payload.get("policy_failed", False)),
        required=bool(payload.get("required", True)),
        metadata=metadata,
        captured_stdout=stdout,
        captured_stderr=stderr,
    )


def _artifact_label_component(value: str) -> str:
    return _ARTIFACT_LABEL_UNSAFE_CHARS.sub("_", value)


def _coverage_result_from_payload(
    payload: Mapping[str, Any],
    *,
    artifacts_dir: Path,
    max_output_bytes: int,
) -> ValidationCoverageResult:
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    command_result_payload = payload.get("command_result")
    command_result = (
        _validation_command_result_from_payload(
            command_result_payload,
            artifacts_dir=artifacts_dir,
            index=999,
            max_output_bytes=max_output_bytes,
        )
        if isinstance(command_result_payload, Mapping)
        else None
    )
    enforce = bool(payload.get("enforce", False))
    status = _coverage_status_from_payload(payload, enforce=enforce)
    gaps = payload.get("gaps", [])
    return ValidationCoverageResult(
        provider=_str_field(payload, "provider"),
        percent=_optional_float(payload.get("percent")),
        minimum_percent=_float_field(payload, "minimum_percent"),
        enforce=enforce,
        status=status,
        reason_code=_str_field(payload, "reason_code"),
        command_result=command_result,
        gaps=gaps if isinstance(gaps, list) else [],
    )


def _coverage_status_from_payload(payload: Mapping[str, Any], *, enforce: bool) -> str:
    status = _str_field(payload, "status").strip().lower()
    if not status:
        raise HostedDelegationProtocolError("hosted delegation response missing status")
    if enforce and status != "passed":
        return "failed"
    return status


def _validate_tool_probe_result_from_terminal(
    payload: Mapping[str, Any],
) -> ValidateToolProbeResult:
    state = _operation_state(payload)
    if state != "succeeded":
        return ValidateToolProbeResult(probe_errored=True, probe_ran=True)
    probe_payload = payload.get("validate_toolchain_probe")
    if not isinstance(probe_payload, Mapping):
        raise HostedDelegationProtocolError(
            "hosted validation response missing validate_toolchain_probe"
        )
    missing_payload = probe_payload.get("missing", [])
    if not isinstance(missing_payload, list):
        raise HostedDelegationProtocolError(
            "hosted validate_toolchain_probe response has malformed missing"
        )
    return ValidateToolProbeResult(
        missing=tuple(_validate_tool_probe_target_from_payload(item) for item in missing_payload),
        probe_errored=bool(probe_payload.get("probe_errored", False)),
        probe_ran=bool(probe_payload.get("probe_ran", False)),
    )


def _validate_tool_probe_target_from_payload(payload: object) -> ValidateCommandProbeTarget:
    if not isinstance(payload, Mapping):
        raise HostedDelegationProtocolError(
            "hosted validate_toolchain_probe missing item is malformed"
        )
    return ValidateCommandProbeTarget(
        tool=_str_field(payload, "tool"),
        command=_str_field(payload, "command"),
    )


def _operation_ref_from_payload(
    payload: Mapping[str, Any],
    *,
    expected_workspace_id: str | None,
    base_url: str,
) -> _HostedOperationRef:
    operation_id = _str_field(payload, "operation_id")
    workspace_id = _optional_str(payload.get("workspace_id"))
    if expected_workspace_id is not None and workspace_id != expected_workspace_id:
        raise HostedDelegationProtocolError("hosted delegation start workspace mismatch")
    raw_url = _str_field(payload, "operation_url")
    operation_url = _normalize_operation_url(raw_url, base_url=base_url)
    return _HostedOperationRef(
        operation_id=operation_id,
        workspace_id=workspace_id,
        url=operation_url,
    )


def _validate_operation_identity(
    payload: Mapping[str, Any],
    operation: _HostedOperationRef,
) -> None:
    if _optional_str(payload.get("operation_id")) != operation.operation_id:
        raise HostedDelegationProtocolError("hosted delegation operation id mismatch")
    if operation.workspace_id is not None and _optional_str(payload.get("workspace_id")) != (
        operation.workspace_id
    ):
        raise HostedDelegationProtocolError("hosted delegation workspace mismatch")


def _operation_state(payload: Mapping[str, Any]) -> str:
    state = payload.get("state", payload.get("status"))
    if not isinstance(state, str) or not state.strip():
        raise HostedDelegationProtocolError("hosted delegation response missing state")
    return state.strip().lower()


def _response_json(response: httpx.Response) -> Mapping[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise HostedDelegationProtocolError("hosted delegation returned non-json response") from exc
    if not isinstance(payload, Mapping):
        raise HostedDelegationProtocolError("hosted delegation returned non-object response")
    return payload


async def _response_json_bounded(
    response: httpx.Response,
    *,
    max_bytes: int,
) -> Mapping[str, Any]:
    content_length = _content_length(response)
    if content_length is not None and content_length > max_bytes:
        raise HostedDelegationProtocolError("hosted delegation response exceeds max_output_bytes")
    chunks: list[bytes] = []
    total_bytes = 0
    async for chunk in response.aiter_bytes():
        total_bytes += len(chunk)
        if total_bytes > max_bytes:
            raise HostedDelegationProtocolError(
                "hosted delegation response exceeds max_output_bytes"
            )
        chunks.append(chunk)
    return _json_payload_from_content(b"".join(chunks))


def _json_payload_from_content(content: bytes) -> Mapping[str, Any]:
    try:
        payload = json.loads(content)
    except ValueError as exc:
        raise HostedDelegationProtocolError("hosted delegation returned non-json response") from exc
    if not isinstance(payload, Mapping):
        raise HostedDelegationProtocolError("hosted delegation returned non-object response")
    return payload


def _content_length(response: httpx.Response) -> int | None:
    value = response.headers.get("Content-Length")
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _response_json_max_bytes(max_output_bytes: int, *, output_slots: int = 1) -> int:
    return (max_output_bytes * max(1, output_slots)) + _HOSTED_RESPONSE_JSON_OVERHEAD_BYTES


def _ensure_output_within_limit(*values: str, max_output_bytes: int) -> None:
    output_bytes = sum(len(value.encode("utf-8")) for value in values)
    if output_bytes > max_output_bytes:
        raise HostedDelegationProtocolError("hosted delegation output exceeds max_output_bytes")


def _str_field(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise HostedDelegationProtocolError(f"hosted delegation response missing {key}")
    return value


def _text_field(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise HostedDelegationProtocolError(f"hosted delegation response missing {key}")
    return value


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _int_field(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise HostedDelegationProtocolError(f"hosted delegation response missing {key}")
    return value


def _float_field(payload: Mapping[str, Any], key: str) -> float:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HostedDelegationProtocolError(f"hosted delegation response missing {key}")
    return float(value)


def _text_payload_field(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key, "")
    if not isinstance(value, str):
        raise HostedDelegationProtocolError(f"hosted delegation response missing {key}")
    return value


def _int_payload_field(value: object, *, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise HostedDelegationProtocolError("hosted delegation response has invalid integer field")
    return value


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HostedDelegationProtocolError("hosted delegation response has invalid float field")
    return float(value)


def _valid_sha(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 40
        and all(c in "0123456789abcdefABCDEF" for c in value)
    )


def _normalize_operation_url(raw_url: str, *, base_url: str) -> str:
    parsed = urlsplit(raw_url)
    if parsed.scheme or parsed.netloc:
        base = urlsplit(base_url)
        if parsed.scheme != base.scheme or parsed.netloc != base.netloc:
            raise HostedDelegationProtocolError("hosted delegation operation_url origin mismatch")
        return raw_url
    if not raw_url.startswith("/"):
        raise HostedDelegationProtocolError("hosted delegation operation_url must be absolute path")
    base = urlsplit(base_url)
    return urlunsplit((base.scheme, base.netloc, parsed.path, parsed.query, parsed.fragment))


def _join_url(base_url: str, path: str) -> str:
    if path.startswith("/"):
        return urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
    return urljoin(base_url.rstrip("/") + "/", path)


async def _cancel_operation(
    client: httpx.AsyncClient,
    config: HostedDelegationConfig,
    operation: _HostedOperationRef,
) -> None:
    try:
        await client.post(
            _join_url(operation.url, "cancel"),
            headers=_auth_headers(config),
            timeout=config.cancel_timeout_seconds,
        )
    except Exception:
        return


def _normalized_url(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().rstrip("/")
    if not normalized:
        return None
    parsed = urlsplit(normalized)
    if parsed.scheme != "https" or not parsed.netloc:
        return None
    return normalized


def _normalized_secret(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _normalized_env_name(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None
