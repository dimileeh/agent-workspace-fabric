"""Hosted delegation contract and configuration helpers.

AWF Core delegates hosted PR-monitor repair and validation through an
authenticated asynchronous HTTP operation protocol. Core remains authoritative
for PR review state, CI state, waits, merge decisions, and audit events; the
hosting control plane only runs short-lived repair/validation jobs against the
existing PR branch.

Operation state machine for AWF Cloud implementers:

1. Core starts an operation with ``POST {base_url}/v1/agent-runs`` or
   ``POST {base_url}/v1/validation-runs``.
   Agent runs normally omit ``git_preparation``; a SyncBase conflict may set
   ``{mode: merge_base, base_ref, expected_base_sha}``, pinned to Core's exact
   fetched base commit.
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
import hashlib
import json
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit

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
from awf.profiles.models import ProfileCoverage, WorkspaceProfile
from awf.runtime.alembic_validation import (
    ALEMBIC_MIGRATION_POLICY_COMMAND,
    ALEMBIC_MIGRATION_POLICY_PHASE,
)
from awf.runtime.hosted_delegation_payloads import (
    _HOSTED_COVERAGE_OMITTED_RUNTIME_ENV,
    _agent_start_payload,
    _hosted_pr_identity_payload,
    _hosted_validation_attach_rendered_stack,
    _hosted_validation_profile_payload,
)
from awf.runtime.hosted_delegation_payloads import (
    _hosted_validation_sanitize_environment_container as _hosted_validation_sanitize_environment_container,
)
from awf.runtime.hosted_delegation_payloads import (
    _hosted_validation_sanitize_secret_refs as _hosted_validation_sanitize_secret_refs,
)
from awf.runtime.validation_coverage import _coverage_reason_code, _coverage_status
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
class _HostedValidationExpectedCommand:
    """One validation command identity expected from the hosted response."""

    phase: str
    command: str
    required: bool
    command_signature: str


_HOSTED_COMMAND_SIGNATURE_PREFIX = "sha256:"
_HOSTED_COMMAND_SIGNATURE_HEX_LEN = 64
_HOSTED_COMMAND_SIGNATURE_PATTERN = re.compile(
    rf"^{re.escape(_HOSTED_COMMAND_SIGNATURE_PREFIX)}[0-9a-f]{{{_HOSTED_COMMAND_SIGNATURE_HEX_LEN}}}$"
)


def _hosted_validation_command_signature(phase: str, command: str) -> str:
    payload = json.dumps([phase, command], ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{_HOSTED_COMMAND_SIGNATURE_PREFIX}{digest}"


def _hosted_validation_command_signature_is_well_formed(signature: object) -> bool:
    if not isinstance(signature, str):
        return False
    return _HOSTED_COMMAND_SIGNATURE_PATTERN.fullmatch(signature) is not None


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
        except Exception:
            if operation is not None:
                await self._cancel_operation(client, operation)
            raise
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
        worktree_path: Path | None = None,
        pr_identity: Mapping[str, Any] | None = None,
    ) -> ValidateToolProbeResult:
        """Delegate the post-setup validate-command toolchain probe to the host."""
        # Same env_file base as run_profile_phases/coverage: repo-relative paths
        # resolve from the worktree so Postgres trust injection matches.
        payload: dict[str, Any] = {
            "workspace_id": workspace_id,
            "profile": _hosted_validation_profile_payload(
                profile,
                compose_dir=compose_file.parent,
                profile_base_path=worktree_path,
            ),
            "phase_names": [],
            "run_healthchecks": False,
            "include_coverage": False,
            "probe": "validate_toolchain",
            "pr_identity": _hosted_pr_identity_payload(pr_identity or {}),
        }
        _hosted_validation_attach_rendered_stack(
            payload,
            compose_project=compose_project,
            compose_file=compose_file,
            include_agent_auth_context=True,
            omit_credential_env_keys=True,
            env_file_base_path=worktree_path,
        )
        terminal = await self._run_operation(
            workspace_id=workspace_id,
            start_path="/v1/validation-runs",
            payload=payload,
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

        # Hosted Jobs use their own /workspace/repo checkout; never send a
        # Core-local filesystem path (Cloud rejects non-null worktree_path).
        # Repo-relative profile env_file paths still resolve from the worktree
        # (same base as profile_services), while compose_dir stays for .env image
        # interpolation.
        profile_payload = _hosted_validation_profile_payload(
            profile,
            compose_dir=compose_file.parent,
            profile_base_path=worktree_path,
            phase_names=phase_names,
        )
        execution_profile = WorkspaceProfile.model_validate(profile_payload)
        expected_commands = _hosted_validation_expected_commands(
            execution_profile,
            phase_names,
            run_healthchecks=run_healthchecks,
        )
        expected_command_count = len(expected_commands)
        payload: dict[str, Any] = {
            "workspace_id": workspace_id,
            "profile": profile_payload,
            "phase_names": list(phase_names),
            "run_healthchecks": run_healthchecks,
            "worktree_path": None,
            "include_coverage": include_coverage,
            "pr_identity": _hosted_pr_identity_payload(pr_identity or {}),
        }
        _hosted_validation_attach_rendered_stack(
            payload,
            compose_project=compose_project,
            compose_file=compose_file,
            include_agent_auth_context=True,
            omit_credential_env_keys=True,
            env_file_base_path=worktree_path,
        )
        terminal = await self._run_operation(
            workspace_id=workspace_id,
            start_path="/v1/validation-runs",
            payload=payload,
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
            expected_commands=expected_commands,
            coverage_policy=profile.validation.coverage if include_coverage else None,
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
        worktree_path: Path | None = None,
        pr_identity: Mapping[str, Any] | None = None,
    ) -> ValidationCoverageResult | None:
        """Delegate a hosted coverage-only operation."""

        # Same env_file base as run_profile_phases: repo-relative paths resolve
        # from the worktree so Postgres trust injection matches phase validation.
        payload: dict[str, Any] = {
            "workspace_id": workspace_id,
            "profile": _hosted_validation_profile_payload(
                profile,
                omit_runtime_environment=_HOSTED_COVERAGE_OMITTED_RUNTIME_ENV,
                compose_dir=compose_file.parent,
                profile_base_path=worktree_path,
            ),
            "phase_names": [phase],
            "run_healthchecks": False,
            "include_coverage": True,
            "parallel_worker_cpu_limit": parallel_worker_cpu_limit,
            "pr_identity": _hosted_pr_identity_payload(pr_identity or {}),
        }
        _hosted_validation_attach_rendered_stack(
            payload,
            compose_project=compose_project,
            compose_file=compose_file,
            include_agent_auth_context=True,
            omit_credential_env_keys=True,
            env_file_base_path=worktree_path,
        )
        terminal = await self._run_operation(
            workspace_id=workspace_id,
            start_path="/v1/validation-runs",
            payload=payload,
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
            command_result_required=profile.validation.coverage.command is not None,
            coverage_policy=profile.validation.coverage,
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
        except Exception:
            if operation is not None:
                await _cancel_operation(client, self._config, operation)
            raise
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
    return len(
        _hosted_validation_expected_commands(
            profile,
            phase_names,
            run_healthchecks=run_healthchecks,
        )
    )


def _hosted_validation_expected_commands(
    profile: WorkspaceProfile,
    phase_names: list[str] | tuple[str, ...],
    *,
    run_healthchecks: bool,
) -> tuple[_HostedValidationExpectedCommand, ...]:
    requested_phases = set(phase_names)
    commands: list[_HostedValidationExpectedCommand] = []
    if "validate" in requested_phases and profile.validation.alembic.enabled:
        commands.append(
            _HostedValidationExpectedCommand(
                phase=ALEMBIC_MIGRATION_POLICY_PHASE,
                command=ALEMBIC_MIGRATION_POLICY_COMMAND,
                required=True,
                command_signature=_hosted_validation_command_signature(
                    ALEMBIC_MIGRATION_POLICY_PHASE,
                    ALEMBIC_MIGRATION_POLICY_COMMAND,
                ),
            )
        )

    healthcheck_commands = [
        _HostedValidationExpectedCommand(
            phase="healthcheck",
            command=healthcheck.display_command(),
            required=True,
            command_signature=_hosted_validation_command_signature(
                "healthcheck",
                healthcheck.display_command(),
            ),
        )
        for healthcheck in profile.validation.healthchecks
    ]
    healthchecks_pending = run_healthchecks and bool(healthcheck_commands)
    healthcheck_before_phase = (
        "validate"
        if profile.database.pre_validation_refresh and "validate" in requested_phases
        else None
    )
    if healthchecks_pending and healthcheck_before_phase is None:
        commands.extend(healthcheck_commands)
        healthchecks_pending = False

    for step in profile_phase_command_plan(profile, phase_names):
        if healthchecks_pending and step.phase == healthcheck_before_phase:
            commands.extend(healthcheck_commands)
            healthchecks_pending = False
        commands.append(
            _HostedValidationExpectedCommand(
                phase=step.phase,
                command=step.command.command,
                required=step.command.required,
                command_signature=_hosted_validation_command_signature(
                    step.phase,
                    step.command.command,
                ),
            )
        )

    if healthchecks_pending:
        commands.extend(healthcheck_commands)

    return tuple(commands)


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
    expected_commands: tuple[_HostedValidationExpectedCommand, ...],
    coverage_policy: ProfileCoverage | None = None,
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
    commands: list[ValidationCommandResult] = []
    for index, item in enumerate(commands_payload, start=1):
        expected = expected_commands[index - 1] if index <= len(expected_commands) else None
        if expected is not None:
            _validate_hosted_validation_command_identity(item, expected=expected)
        required = expected.required if expected is not None else None
        commands.append(
            _validation_command_result_from_payload(
                item,
                artifacts_dir=artifacts_dir,
                index=index,
                max_output_bytes=max_output_bytes,
                required=required,
            )
        )
    expected_command_count = len(expected_commands)
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
            coverage_policy=coverage_policy,
        )
        if isinstance(coverage_payload, Mapping)
        else None
    )
    return ValidationResult(commands=commands, coverage=coverage)


def _validate_hosted_validation_command_identity(
    payload: object,
    *,
    expected: _HostedValidationExpectedCommand,
) -> None:
    if not isinstance(payload, Mapping):
        raise HostedDelegationProtocolError("hosted validation command result is malformed")
    phase = _optional_str(payload.get("phase")) or "validate"
    if phase != expected.phase:
        raise HostedDelegationProtocolError("hosted validation command identity mismatch")
    command_signature = payload.get("command_signature")
    if command_signature not in (None, ""):
        if not _hosted_validation_command_signature_is_well_formed(command_signature):
            raise HostedDelegationProtocolError("hosted validation command signature is malformed")
        if command_signature != expected.command_signature:
            raise HostedDelegationProtocolError("hosted validation command signature mismatch")
        return
    command = _optional_str(payload.get("command"))
    if command != expected.command:
        raise HostedDelegationProtocolError("hosted validation command identity mismatch")


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
    required: bool | None = None,
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
        required=bool(payload.get("required", True)) if required is None else required,
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
    command_result_required: bool = False,
    coverage_policy: ProfileCoverage | None = None,
) -> ValidationCoverageResult:
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    command_result_payload = payload.get("command_result")
    if command_result_required and not isinstance(command_result_payload, Mapping):
        raise HostedDelegationProtocolError(
            "hosted validation terminal response missing command evidence"
        )
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
    payload_enforce = bool(payload.get("enforce", False))
    payload_minimum_percent = _float_field(payload, "minimum_percent")
    minimum_percent = (
        coverage_policy.minimum_percent if coverage_policy is not None else payload_minimum_percent
    )
    enforce = coverage_policy.enforce if coverage_policy is not None else payload_enforce
    payload_status = _coverage_status_from_payload(payload, enforce=payload_enforce)
    status = _coverage_status_from_payload(payload, enforce=enforce)
    percent = _optional_float(payload.get("percent"))
    reason_code = _str_field(payload, "reason_code")
    if coverage_policy is not None:
        policy_reason_code = _coverage_reason_code(
            percent=percent,
            minimum_percent=minimum_percent,
            command_result=command_result,
        )
        if policy_reason_code != "COVERAGE_OK":
            reason_code = policy_reason_code
            status = _coverage_status(reason_code=policy_reason_code, enforce=enforce)
        elif reason_code != "COVERAGE_OK":
            status = _coverage_status(reason_code=reason_code, enforce=enforce)
        elif payload_status not in {"passed", "reported"}:
            status = "failed" if enforce else payload_status
        else:
            status = "passed"
    gaps = payload.get("gaps", [])
    return ValidationCoverageResult(
        provider=_str_field(payload, "provider"),
        percent=percent,
        minimum_percent=minimum_percent,
        enforce=enforce,
        status=status,
        reason_code=reason_code,
        command_result=command_result,
        gaps=gaps if isinstance(gaps, list) else [],
        failing_test_node_ids=_string_list_from_payload(payload.get("failing_test_node_ids")),
        failing_test_evidence=_string_list_from_payload(payload.get("failing_test_evidence")),
        provider_failure_evidence=_string_list_from_payload(
            payload.get("provider_failure_evidence")
        ),
        parallel_workers_requested=_optional_int_from_payload(
            payload.get("parallel_workers_requested")
        ),
        parallel_workers_effective=_optional_int_from_payload(
            payload.get("parallel_workers_effective")
        ),
        parallel_distribution=_optional_str(payload.get("parallel_distribution")),
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


def _string_list_from_payload(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _optional_int_from_payload(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
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
        try:
            same_origin = _url_origin(parsed) == _url_origin(base)
        except ValueError as exc:
            raise HostedDelegationProtocolError(
                "hosted delegation operation_url origin mismatch"
            ) from exc
        if not same_origin:
            raise HostedDelegationProtocolError("hosted delegation operation_url origin mismatch")
        return raw_url
    if not raw_url.startswith("/"):
        raise HostedDelegationProtocolError("hosted delegation operation_url must be absolute path")
    base = urlsplit(base_url)
    return urlunsplit((base.scheme, base.netloc, parsed.path, parsed.query, parsed.fragment))


def _url_origin(parsed: SplitResult) -> tuple[str, str, int | None]:
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("origin cannot include userinfo")
    hostname = parsed.hostname or ""
    return parsed.scheme, hostname, _effective_url_port(parsed)


def _effective_url_port(parsed: SplitResult) -> int | None:
    port = parsed.port
    if port is not None:
        return port
    return {"https": 443, "http": 80}.get(parsed.scheme)


def _join_url(base_url: str, path: str) -> str:
    if path.startswith("/"):
        return urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
    return urljoin(base_url.rstrip("/") + "/", path)


def _append_url_path_segment(base_url: str, segment: str) -> str:
    parsed = urlsplit(base_url)
    base_path = parsed.path.rstrip("/")
    suffix = segment.strip("/")
    path = f"{base_path}/{suffix}" if base_path else f"/{suffix}"
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment))


async def _cancel_operation(
    client: httpx.AsyncClient,
    config: HostedDelegationConfig,
    operation: _HostedOperationRef,
) -> None:
    try:
        await client.post(
            _append_url_path_segment(operation.url, "cancel"),
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
    if parsed.username is not None or parsed.password is not None:
        return None
    if parsed.query or parsed.fragment:
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
