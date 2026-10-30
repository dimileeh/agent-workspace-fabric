"""Checkpoint intent and private producer metadata, not lifecycle HTTP responses."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator

from awf.hosted_node.schemas import Generation, Identity, Opaque, StrictModel

Digest = Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")]
CHECKPOINT_SCHEMA_REVISION = "b830e6d9a142"


class CheckpointRequest(StrictModel):
    checkpoint_id: str
    request_sha256: Digest

    @field_validator("checkpoint_id")
    @classmethod
    def canonical_id(cls, value: str) -> str:
        parsed = UUID(value)
        if str(parsed) != value or parsed.int == 0:
            raise ValueError("CANONICAL_CHECKPOINT_UUID_REQUIRED")
        return value


class CheckpointMarker(CheckpointRequest):
    version: Literal[1] = 1
    maintenance_generation: Generation
    identity: Identity
    disk_ref: Opaque
    database_ref: Opaque
    schema_revision: Opaque
    prepared_at: AwareDatetime


def marker_payload(
    request: CheckpointRequest,
    identity: Identity,
    generation: int,
    disk_ref: str,
    database_ref: str,
    prepared_at: datetime,
) -> CheckpointMarker:
    return CheckpointMarker(
        **request.model_dump(),
        maintenance_generation=generation,
        identity=identity,
        disk_ref=disk_ref,
        database_ref=database_ref,
        schema_revision=CHECKPOINT_SCHEMA_REVISION,
        prepared_at=prepared_at,
    )
