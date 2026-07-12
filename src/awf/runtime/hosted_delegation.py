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
5. Agent repair terminal responses must include ``returncode``, ``stdout``,
   ``stderr``, and optional ``timeout_reason``. Successful agent repair
   terminals must also include ``terminal_head_sha`` for the remote PR head
   pushed by the host. Core fetches the PR branch and verifies that SHA before
   monitor bookkeeping continues.
6. Validation terminal responses return Core-compatible validation command
   results; CI remains a separate required merge gate.

Bodies are secret-free. Prompt text is sent only in the request body field
reserved for stdin, never argv, query strings, or logs. Provider credentials
and delegation bearer tokens are never serialized into hosted requests.
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from awf.adapters.runtime_executor import (
    _HOSTED_TIMEOUT_RETURN_CODE,
    AgentRuntimeExecRequest,
    AgentRuntimeExecResult,
    AgentRuntimeExecutor,
)
from awf.common.commands import COMMAND_TIMEOUT_REASON
from awf.common.config import Settings
from awf.profiles.models import WorkspaceProfile
from awf.runtime.validation_types import (
    ValidationCommandResult,
    ValidationCoverageResult,
    ValidationResult,
)

HOSTED_DELEGATION_MISSING_BASE_URL = "AWF_HOSTED_DELEGATION_BASE_URL"
HOSTED_DELEGATION_MISSING_TOKEN = (
    "AWF_HOSTED_DELEGATION_BEARER_TOKEN or AWF_HOSTED_DELEGATION_BEARER_TOKEN_ENV"
)
_ARTIFACT_LABEL_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9_-]+")


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

    env = os.environ if environ is None else environ
    missing: list[str] = []
    base_url = _normalized_url(settings.hosted_delegation_base_url)
    if base_url is None:
        missing.append(HOSTED_DELEGATION_MISSING_BASE_URL)
    token = _normalized_secret(settings.hosted_delegation_bearer_token)
    token_env = _normalized_env_name(settings.hosted_delegation_bearer_token_env)
    if token is None and token_env is not None:
        token = _normalized_secret(env.get(token_env))
    if token is None:
        missing.append(HOSTED_DELEGATION_MISSING_TOKEN)
    if missing:
        raise HostedDelegationConfigError(missing=tuple(missing))
    assert base_url is not None
    assert token is not None
    return HostedDelegationConfig(
        base_url=base_url,
        bearer_token=token,
        poll_interval_seconds=settings.hosted_delegation_poll_interval_seconds,
        operation_timeout_seconds=settings.hosted_delegation_operation_timeout_seconds,
        request_timeout_seconds=settings.hosted_delegation_request_timeout_seconds,
        cancel_timeout_seconds=settings.hosted_delegation_cancel_timeout_seconds,
        max_output_bytes=settings.hosted_delegation_max_output_bytes,
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
        return _agent_result_from_terminal(terminal)

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
            response = await client.get(
                operation.url,
                headers=_auth_headers(self._config),
                timeout=self._config.request_timeout_seconds,
            )
            response.raise_for_status()
            payload = _response_json(response)
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
        terminal = await self._run_operation(
            workspace_id=workspace_id,
            start_path="/v1/validation-runs",
            payload={
                "workspace_id": workspace_id,
                "profile": profile.model_dump(mode="json", by_alias=True),
                "phase_names": list(phase_names),
                "run_healthchecks": run_healthchecks,
                "worktree_path": str(worktree_path) if worktree_path is not None else None,
                "include_coverage": include_coverage,
                "pr_identity": dict(pr_identity or {}),
            },
        )
        return _validation_result_from_terminal(
            terminal,
            artifacts_dir=self._artifacts_dir / workspace_id,
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
                "profile": profile.model_dump(mode="json", by_alias=True),
                "phase_names": [phase],
                "run_healthchecks": False,
                "include_coverage": True,
                "parallel_worker_cpu_limit": parallel_worker_cpu_limit,
                "pr_identity": dict(pr_identity or {}),
            },
        )
        coverage = terminal.get("coverage")
        if coverage is None:
            return None
        if not isinstance(coverage, Mapping):
            raise HostedDelegationProtocolError(
                "hosted validation terminal response has malformed coverage"
            )
        return _coverage_result_from_payload(
            coverage,
            artifacts_dir=self._artifacts_dir / workspace_id,
        )

    async def _run_operation(
        self,
        *,
        workspace_id: str,
        start_path: str,
        payload: Mapping[str, Any],
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
            return await self._poll_operation(client, operation)
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
    ) -> Mapping[str, Any]:
        deadline = time.monotonic() + self._config.operation_timeout_seconds
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError
            response = await client.get(
                operation.url,
                headers=_auth_headers(self._config),
                timeout=self._config.request_timeout_seconds,
            )
            response.raise_for_status()
            payload = _response_json(response)
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
            identity[key] = value
    if request.owned_paths:
        identity["owned_paths"] = list(request.owned_paths)
    return identity


def _agent_result_from_terminal(payload: Mapping[str, Any]) -> AgentRuntimeExecResult:
    return AgentRuntimeExecResult(
        returncode=_int_field(payload, "returncode"),
        stdout=_text_field(payload, "stdout"),
        stderr=_text_field(payload, "stderr"),
        timeout_reason=_optional_str(payload.get("timeout_reason")) or "",
        terminal_head_sha=_optional_str(payload.get("terminal_head_sha")),
    )


def _validation_result_from_terminal(
    payload: Mapping[str, Any],
    *,
    artifacts_dir: Path,
) -> ValidationResult:
    commands_payload = payload.get("commands", [])
    if not isinstance(commands_payload, list):
        raise HostedDelegationProtocolError("hosted validation response has malformed commands")
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    commands = [
        _validation_command_result_from_payload(
            item,
            artifacts_dir=artifacts_dir,
            index=index,
        )
        for index, item in enumerate(commands_payload, start=1)
    ]
    coverage_payload = payload.get("coverage")
    coverage = (
        _coverage_result_from_payload(coverage_payload, artifacts_dir=artifacts_dir)
        if isinstance(coverage_payload, Mapping)
        else None
    )
    return ValidationResult(commands=commands, coverage=coverage)


def _validation_command_result_from_payload(
    payload: object,
    *,
    artifacts_dir: Path,
    index: int,
) -> ValidationCommandResult:
    if not isinstance(payload, Mapping):
        raise HostedDelegationProtocolError("hosted validation command result is malformed")
    phase = _optional_str(payload.get("phase")) or "validate"
    label = f"{index:02d}_{_artifact_label_component(phase)}"
    stdout = _text_payload_field(payload, "stdout")
    stderr = _text_payload_field(payload, "stderr")
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
) -> ValidationCoverageResult:
    command_result_payload = payload.get("command_result")
    command_result = (
        _validation_command_result_from_payload(
            command_result_payload,
            artifacts_dir=artifacts_dir,
            index=999,
        )
        if isinstance(command_result_payload, Mapping)
        else None
    )
    gaps = payload.get("gaps", [])
    return ValidationCoverageResult(
        provider=_str_field(payload, "provider"),
        percent=_optional_float(payload.get("percent")),
        minimum_percent=_float_field(payload, "minimum_percent"),
        enforce=bool(payload.get("enforce", False)),
        status=_str_field(payload, "status"),
        reason_code=_str_field(payload, "reason_code"),
        command_result=command_result,
        gaps=gaps if isinstance(gaps, list) else [],
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
    return _join_url(base_url, raw_url)


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
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
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
