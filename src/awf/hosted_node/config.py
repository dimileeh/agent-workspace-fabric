"""Trusted server-only hosted settings. No request can enable protected mode."""

from pathlib import Path

from pydantic import Field, SecretStr, model_validator

from awf.hosted_node.host_config import HostSettings
from awf.hosted_node.lifecycle_schemas import LifecycleSettings
from awf.hosted_node.schemas import Generation, LogicalOwner, Opaque, StrictModel


class HostedSettings(StrictModel):
    owner: LogicalOwner
    incarnation: Opaque
    recovery_generation: Generation
    controller_token: SecretStr = Field(min_length=16)
    material_root: Path
    lifecycle: LifecycleSettings | None = None
    host: HostSettings | None = None

    @model_validator(mode="after")
    def physical_identity(self) -> "HostedSettings":
        if self.host is not None and self.lifecycle is None:
            raise ValueError("HOST_LIFECYCLE_REQUIRED")
        if self.lifecycle is not None and (
            not self.incarnation.isascii()
            or not self.incarnation.isdecimal()
            or not 0 < int(self.incarnation) < 2**64
            or str(int(self.incarnation)) != self.incarnation
        ):
            raise ValueError("NUMERIC_PHYSICAL_ID_REQUIRED")
        return self
