"""Public lifecycle metadata and server-injected, short-lived evidence."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import field_validator

from awf.hosted_node.schemas import Generation, Identity, Opaque, StrictModel


class LifecycleSettings(StrictModel):
    disk_ref: Opaque
    database_ref: Opaque


class RecoveryAcknowledgement(StrictModel):
    idempotency_key: Opaque
    receipt_ref: Opaque


class Drain(StrictModel):
    generation: Generation


class BootChallenge(StrictModel):
    challenge: str

    @field_validator("challenge")
    @classmethod
    def canonical_nonce(cls, value: str) -> str:
        if len(value) != 36 or str(UUID(value)) != value or UUID(value).int == 0:
            raise ValueError("canonical nonzero UUID required")
        return value


class BootEvidence(BootChallenge):
    identity: Identity
    previous_identity: Identity | None
    kernel_boot_id: str
    disk_ref: Opaque
    database_ref: Opaque
    recovery_acknowledged: bool
    admission_closed: bool
    runtime_ready: Literal[False] = False
    expires_at: datetime


@dataclass(frozen=True)
class HostBootObservation:
    identity: Identity
    kernel_boot_id: str
    disk_ref: str
    database_ref: str
    expires_at: datetime


class LifecycleStatus(StrictModel):
    identity: Identity
    state: Literal["recovering", "ready", "draining", "drained"]
    admission_closed: bool
    runtime_ready: bool = False
    node_ready: bool
    quiescent: bool
    database_quiescent: bool
    idle_since: datetime | None
    drain_generation: int | None
    expires_at: datetime | None


@dataclass(frozen=True)
class HostObservation:
    identity: Identity
    expires_at: datetime
    quiescent: bool
    retained_state_verified: bool


@dataclass(frozen=True)
class RecoveryEvidence:
    identity: Identity
    previous_identity: Identity | None
    disk_ref: str
    database_ref: str
    receipt_ref: str
    expires_at: datetime
