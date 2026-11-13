"""Public lifecycle metadata and server-injected, short-lived evidence."""

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, field_validator

from awf.hosted_node.checkpoint_schemas import CheckpointRequest
from awf.hosted_node.schemas import Generation, Identity, Opaque, StrictModel


class LifecycleSettings(StrictModel):
    disk_ref: Opaque
    database_ref: Opaque


class RecoveryAcknowledgement(StrictModel):
    idempotency_key: Opaque
    receipt_ref: Opaque


class Drain(StrictModel):
    generation: Generation


class Maintenance(StrictModel):
    """Monotonic hold token, independent of idle-drain generations."""

    generation: Generation


class MaintenanceBegin(Maintenance):
    checkpoint: CheckpointRequest | None = None


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


class RecoveryAcceptance(RecoveryAcknowledgement):
    """Immutable committed reconciliation snapshot for this exact boot."""

    previous_identity: Identity | None
    disk_ref: Opaque
    database_ref: Opaque
    queued_workspaces: Annotated[int, Field(strict=True, ge=0, le=9223372036854775807)]
    monitor_workspaces: Annotated[int, Field(strict=True, ge=0, le=9223372036854775807)]
    attention_workspaces: Annotated[int, Field(strict=True, ge=0, le=9223372036854775807)]


QuiescenceBlockerName = Literal[
    "WORKSPACE_ACTIVE",
    "WORKSPACE_CLAIMED",
    "COMPOSE_NOT_RELEASED",
    "ATTEMPT_ACTIVE",
    "OPERATION_ACTIVE",
    "VALIDATION_ACTIVE",
    "RESERVATION_HELD",
    "GC_PENDING",
    "CALLBACK_PENDING",
    "MERGE_CANDIDATE_OPEN",
    "SECRET_LEASE_ACTIVE",
    "DOCKER_UNEXPECTED_CONTAINER",
    "CGROUP_POPULATED",
]


class QuiescenceBlocker(StrictModel):
    """Why the node is not idle: a fixed name and a row count, never row ids."""

    name: QuiescenceBlockerName
    count: Annotated[int, Field(strict=True, ge=1, le=9223372036854775807)]


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
    maintenance_generation: int | None = None
    maintenance_released_through: int = 0
    recovery_acceptance: RecoveryAcceptance | None = None
    expires_at: datetime | None
    # Additive: older payloads omit it. Names and counts only, never row ids.
    blockers: list[QuiescenceBlocker] = []


@dataclass(frozen=True)
class HostObservation:
    identity: Identity
    expires_at: datetime
    quiescent: bool
    retained_state_verified: bool
    blockers: tuple[QuiescenceBlockerName, ...] = ()


@dataclass(frozen=True)
class RecoveryEvidence:
    identity: Identity
    previous_identity: Identity | None
    disk_ref: str
    database_ref: str
    receipt_ref: str
    expires_at: datetime
