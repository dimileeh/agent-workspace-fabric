"""Per-invocation managed provider credentials for agent adapters.

Adapters stay generic: a ``ProviderCredentialBroker`` (implemented in
``awf.service.provider_managed_keys``) is injected like the usage sampler. A
lease's key value lives only in the exec subprocess environment of the
invocation that acquired it; it never enters compose files, argv, the DB,
events or logs.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from awf.common.agent_execution import ExecutionSelection

MANAGED_CREDENTIAL_ENV = "OPENROUTER_API_KEY"


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
