"""Trusted server-only hosted settings. No request can enable protected mode."""

from pathlib import Path

from pydantic import Field, SecretStr

from awf.hosted_node.schemas import Generation, Opaque, StrictModel


class HostedSettings(StrictModel):
    owner: Opaque
    incarnation: Opaque
    recovery_generation: Generation
    controller_token: SecretStr = Field(min_length=16)
    material_root: Path
