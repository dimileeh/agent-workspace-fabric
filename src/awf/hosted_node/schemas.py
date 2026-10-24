"""Bounded public metadata; material is excluded from representations and persistence."""

from datetime import datetime
from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Opaque = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")]
LogicalOwner = Annotated[
    str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*$")
]
Generation = Annotated[int, Field(strict=True, ge=1, le=9223372036854775807)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class HostedErrorResponse(StrictModel):
    error_code: str


class Identity(StrictModel):
    owner: LogicalOwner
    incarnation: Opaque
    recovery_generation: Generation
    boot_epoch: Generation

    def headers(self) -> dict[str, str]:
        return {
            "X-AWF-Expected-" + key.replace("_", "-").title(): str(value)
            for key, value in self.model_dump().items()
        }


class IdentityStatus(Identity):
    activation: Literal["closed"] = "closed"
    acknowledged: bool


class Acknowledgement(StrictModel):
    idempotency_key: Opaque


class Preparation(StrictModel):
    namespace: Opaque
    workspace_id: Opaque
    preparation_id: Opaque
    purpose: Literal["credential"]
    target: str = Field(max_length=256)
    expires_at: datetime

    @field_validator("target")
    @classmethod
    def safe_target(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            not value.startswith("/run/awf/secrets/")
            or str(path) != value
            or ".." in path.parts
            or value.endswith("/")
            or any(not (c.isascii() and (c.isalnum() or c in "/_.-")) for c in value)
        ):
            raise ValueError("INVALID_TARGET")
        return value

    @field_validator("expires_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("UTC_EXPIRY_REQUIRED")
        return value


class Binding(Preparation):
    identity: Identity
    idempotency_key: Opaque
    grant_id: Opaque
    revision: Generation
    authorization_generation: Generation
    fence: Generation


class Install(Binding):
    material: str = Field(min_length=4, max_length=87384, repr=False, exclude=True)


class Refresh(Install):
    predecessor: Opaque


class MaterialStatus(StrictModel):
    status: Literal["current", "not_accepted", "revoked", "expired", "unavailable"]
    receipt_id: str | None = None
    revision: int
    reason: str
