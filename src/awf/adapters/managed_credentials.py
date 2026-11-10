"""Per-invocation managed provider credentials for agent adapters.

Adapters stay generic: a ``ProviderCredentialBroker`` (implemented in
``awf.service.provider_managed_keys``) is injected like the usage sampler. A
lease's key value lives only in the exec subprocess environment of the
invocation that acquired it; it never enters compose files, argv, the DB,
events or logs.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from awf.adapters.provider_failures import is_budget_exhausted_output
from awf.adapters.run_results import AgentRunError
from awf.common.commands import CommandResult
from awf.common.compose_exec import mark_masked_agent_reason_code
from awf.common.redaction import redact_secrets

if TYPE_CHECKING:
    from awf.common.agent_execution import ExecutionSelection

MANAGED_CREDENTIAL_ENV = "OPENROUTER_API_KEY"
PROVIDER_BUDGET_EXHAUSTED = "PROVIDER_BUDGET_EXHAUSTED"
PROVIDER_MANAGEMENT_KEY_MISSING = "PROVIDER_MANAGEMENT_KEY_MISSING"
PROVIDER_MANAGED_KEY_HOSTED_UNSUPPORTED = "PROVIDER_MANAGED_KEY_HOSTED_UNSUPPORTED"
PROVIDER_MANAGED_KEY_UNAVAILABLE = "PROVIDER_MANAGED_KEY_UNAVAILABLE"
PROVIDER_PARK_REASON_CODES = frozenset(
    {PROVIDER_BUDGET_EXHAUSTED, PROVIDER_MANAGED_KEY_UNAVAILABLE, PROVIDER_MANAGEMENT_KEY_MISSING}
)
"""Managed-key failures that park a workspace for a human: never failed, retried
blindly, counted against the shared circuit, or sent to a fallback provider."""
_WATCHDOG_TIMEOUT_REASON_CODES = frozenset({"AGENT_TIMEOUT", "AGENT_IDLE_TIMEOUT"})
_BUDGET_TEXT_TAIL_CHARS = 2000


def exec_environment(credential_env: Mapping[str, str]) -> dict[str, str]:
    """The exec subprocess env: the worker env plus the minted key, never the management key."""
    import os

    inherited = {k: v for k, v in os.environ.items() if k != "OPENROUTER_MANAGEMENT_KEY"}
    return {**inherited, **credential_env}


class ProviderCredentialError(Exception):
    """A managed credential could not be provided; never carries secret values."""

    def __init__(self, reason_code: str, *, details: Mapping[str, Any] | None = None) -> None:
        self.reason_code = reason_code
        self.details: dict[str, Any] = dict(details or {})
        super().__init__(reason_code)


@dataclass(frozen=True)
class ProviderCredentialRelease:
    """Final verdict of one released lease (non-secret budget figures only)."""

    exhausted: bool
    details: dict[str, Any] = field(default_factory=dict)


class ProviderCredentialLease(Protocol):  # pragma: no cover - Protocol declaration only.
    @property
    def env(self) -> Mapping[str, str]: ...

    @property
    def secrets(self) -> tuple[str, ...]: ...

    async def heartbeat(self) -> None: ...

    async def release(self, *, failed: bool) -> ProviderCredentialRelease: ...


class ProviderCredentialBroker(Protocol):  # pragma: no cover - Protocol declaration only.
    @property
    def heartbeat_interval_seconds(self) -> float: ...

    async def acquire(
        self, *, workspace_id: str, selection: ExecutionSelection, purpose: str
    ) -> ProviderCredentialLease: ...


def credential_error(
    adapter: Any, reason_code: str, details: Mapping[str, Any] | None = None
) -> AgentRunError:
    """A non-retryable agent error for a credential that could not be provided."""
    return AgentRunError(
        agent=adapter.name,
        result=CommandResult(returncode=78, stdout="", stderr=reason_code),
        reason_code=reason_code,
        details={**_selection_details(adapter), **dict(details or {}), "retryable": False},
    )


def budget_exhausted_error(
    adapter: Any,
    error: AgentRunError,
    release: ProviderCredentialRelease,
    *,
    secrets: tuple[str, ...],
) -> AgentRunError:
    """Replace a run failure caused by the exhausted key with the park code.

    The provider-recovery metadata is dropped on purpose: an exhausted budget is
    never retried blindly and never falls back to another provider.
    """
    result = CommandResult(
        returncode=error.result.returncode,
        stdout=redact_secrets(error.result.stdout, extra_secrets=secrets),
        stderr=redact_secrets(error.result.stderr, extra_secrets=secrets),
        reason_code=error.result.reason_code,
    )
    return AgentRunError(
        agent=error.agent,
        result=result,
        reason_code=PROVIDER_BUDGET_EXHAUSTED,
        details={
            **_selection_details(adapter),
            "retryable": False,
            "source_reason_code": error.reason_code,
            "provider_budget": dict(release.details),
        },
    )


@asynccontextmanager
async def managed_credential_scope(
    adapter: Any,
    *,
    workspace_id: str | None,
    purpose: str,
    lease: ProviderCredentialLease | None = None,
) -> AsyncIterator[ProviderCredentialLease | None]:
    """Hold one managed key for one invocation, then final-read and delete it.

    Yields ``None`` for every selection that is not a managed OpenRouter key, so
    static-key, Ollama, native and OpenCode runs are unchanged. A caller-owned
    ``lease`` (a diagnostic's probe and run share one) is used as-is; its owner
    releases it.
    """
    selection = getattr(adapter, "execution", None)
    if selection is None or not selection.managed_key:
        yield None
        return
    if adapter.is_hosted:
        raise credential_error(adapter, PROVIDER_MANAGED_KEY_HOSTED_UNSUPPORTED)
    if lease is not None:
        yield lease
        return
    broker: ProviderCredentialBroker | None = adapter._credential_broker
    if broker is None or workspace_id is None:
        raise credential_error(adapter, PROVIDER_MANAGEMENT_KEY_MISSING)
    try:
        owned = await broker.acquire(
            workspace_id=workspace_id, selection=selection, purpose=purpose
        )
    except ProviderCredentialError as error:
        raise credential_error(adapter, error.reason_code, error.details) from None
    heartbeat = asyncio.create_task(_heartbeat(owned, broker.heartbeat_interval_seconds))
    try:
        yield owned
    except AgentRunError as error:
        release = await _finish(owned, heartbeat, failed=True, original=error)
        if error.reason_code not in _WATCHDOG_TIMEOUT_REASON_CODES and _exhausted_by_key(
            release, error
        ):
            raise budget_exhausted_error(adapter, error, release, secrets=owned.secrets) from None
        raise
    except BaseException as error:
        await _finish(owned, heartbeat, failed=True, original=error)
        raise
    else:
        await _finish(owned, heartbeat, failed=False, original=None)


def _exhausted_by_key(release: ProviderCredentialRelease, error: AgentRunError) -> bool:
    """The final key read decides; only when it failed does the stderr tail count.

    The transcript is never scanned: a task editing payment code (or AWF's own
    "payment required" text) must not be relabelled as an exhausted budget.
    """
    if release.exhausted:
        return True
    if release.details.get("final_usage_read") is True:
        return False
    return is_budget_exhausted_output(error.result.stderr[-_BUDGET_TEXT_TAIL_CHARS:])


async def _heartbeat(lease: ProviderCredentialLease, interval_seconds: float) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        await lease.heartbeat()


async def _finish(
    lease: ProviderCredentialLease,
    heartbeat: asyncio.Task[None],
    *,
    failed: bool,
    original: BaseException | None,
) -> ProviderCredentialRelease:
    """Stop usage polling, then release; a cancelled caller never strands the key."""
    heartbeat.cancel()

    async def _release() -> ProviderCredentialRelease:
        # The final read must land after the last periodic read, never before.
        await asyncio.wait({heartbeat})
        return await lease.release(failed=failed)

    pending = asyncio.ensure_future(_release())
    try:
        return await asyncio.shield(pending)
    except asyncio.CancelledError as cancel:
        masked = (
            original.reason_code
            if isinstance(original, AgentRunError)
            and original.reason_code in _WATCHDOG_TIMEOUT_REASON_CODES
            else getattr(original, "agent_reason_code", None)
        )
        if isinstance(masked, str):
            mark_masked_agent_reason_code(cancel, masked)
        raise


def _selection_details(adapter: Any) -> dict[str, Any]:
    selection = adapter.execution
    return {
        "provider": selection.provider,
        "model": selection.model,
        "execution": selection.model_dump(mode="json"),
    }
