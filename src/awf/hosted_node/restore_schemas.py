"""Server-only authority evidence. No HTTP request can select restore mode."""

from dataclasses import dataclass
from datetime import datetime

from pydantic import AwareDatetime, model_validator

from awf.hosted_node.checkpoint_schemas import CheckpointMarker, CheckpointRequest, Digest
from awf.hosted_node.schemas import Generation, Identity, Opaque, StrictModel


class RestoreBinding(StrictModel):
    restore_id: str
    checkpoint: CheckpointMarker
    manifest_sha256: Digest
    files_sha256: Digest
    schema_sha256: Digest
    point_in_time: AwareDatetime
    storage_mapping_sha256: Digest
    fenced_identity: Identity
    candidate_incarnation: Opaque
    candidate_recovery_generation: Generation
    boot_epoch_floor: Generation
    target_disk_ref: Opaque
    target_database_ref: Opaque

    @model_validator(mode="after")
    def exact_scope(self) -> "RestoreBinding":
        CheckpointRequest.canonical_id(self.restore_id)
        prior = self.checkpoint.identity
        if (
            prior.owner != self.fenced_identity.owner
            or self.candidate_recovery_generation
            <= max(prior.recovery_generation, self.fenced_identity.recovery_generation)
            or self.boot_epoch_floor < max(prior.boot_epoch, self.fenced_identity.boot_epoch)
            or self.boot_epoch_floor >= 9223372036854775807
            or self.candidate_incarnation == self.fenced_identity.incarnation
            or self.point_in_time < self.checkpoint.prepared_at
        ):
            raise ValueError("RESTORE_AUTHORITY_SCOPE_INVALID")
        return self


@dataclass(frozen=True)
class RestoreBootEvidence:
    """Produced only after current authority and actual restored cut verification."""

    binding: RestoreBinding
    expires_at: datetime


@dataclass(frozen=True)
class RestoreEvidence:
    """Fresh final authority pins the actual challenged post-boot identity."""

    binding: RestoreBinding
    identity: Identity
    receipt_ref: str
    expires_at: datetime
