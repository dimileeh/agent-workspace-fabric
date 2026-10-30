"""Server-owned Linux host observation inputs, never request configuration."""

import re
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field, SecretStr, field_validator, model_validator

from awf.hosted_node.schemas import Opaque, StrictModel


class ControlContainer(StrictModel):
    id: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    image: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]


class PhysicalFenceAuthoritySettings(StrictModel):
    """Provisioned node-read credential and fixed endpoint, never public input."""

    url: str = Field(strict=True, min_length=1, max_length=2048)
    bearer: SecretStr = Field(min_length=16, max_length=4096, repr=False, exclude=True)

    @field_validator("url")
    @classmethod
    def fixed_https_endpoint(cls, value: str) -> str:
        if any(not 33 <= ord(c) <= 126 for c in value):
            raise ValueError("FIXED_HTTPS_AUTHORITY_REQUIRED")
        try:
            parts = urlsplit(value)
            valid = (
                parts.scheme == "https"
                and parts.hostname
                and parts.port != 0
                and parts.username is None
                and parts.password is None
                and not any(c in value for c in "?#\\")
            )
        except ValueError:
            raise ValueError("FIXED_HTTPS_AUTHORITY_REQUIRED") from None
        if not valid:
            raise ValueError("FIXED_HTTPS_AUTHORITY_REQUIRED")
        return value

    @field_validator("bearer")
    @classmethod
    def bearer_header(cls, value: SecretStr) -> SecretStr:
        if re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", value.get_secret_value()) is None:
            raise ValueError("NODE_AUTHORITY_BEARER_REQUIRED")
        return value


class HostSettings(StrictModel):
    state_root: Path
    device_path: Path
    filesystem_uuid: str
    workspaces_root: Path
    docker_root: Path
    execution_cgroup: Path
    identity_file: Path
    bootstrap_approval: Path | None = None
    docker_socket: Path
    database_name: Opaque
    database_role: Opaque
    database_schema: Opaque = "public"
    allowed_containers: Annotated[tuple[ControlContainer, ...], Field(max_length=4)] = ()
    physical_fence_authority: PhysicalFenceAuthoritySettings | None = None

    @field_validator("filesystem_uuid")
    @classmethod
    def canonical_uuid(cls, value: str) -> str:
        if str(UUID(value)) != value:
            raise ValueError("CANONICAL_FILESYSTEM_UUID_REQUIRED")
        return value

    @model_validator(mode="after")
    def paths(self) -> "HostSettings":
        for path in (
            self.state_root,
            self.device_path,
            self.workspaces_root,
            self.docker_root,
            self.execution_cgroup,
            self.identity_file,
            self.bootstrap_approval,
            self.docker_socket,
        ):
            if path is not None and (
                not path.is_absolute() or ".." in path.parts or path == Path("/")
            ):
                raise ValueError("ABSOLUTE_HOST_PATH_REQUIRED")
        if (
            not self.device_path.is_relative_to("/dev")
            or not self.execution_cgroup.is_relative_to("/sys/fs/cgroup")
            or self.execution_cgroup == Path("/sys/fs/cgroup")
            or not self.workspaces_root.is_relative_to(self.state_root)
            or self.workspaces_root == self.state_root
            or not self.docker_root.is_relative_to(self.state_root)
            or self.docker_root == self.state_root
            or len({x.id for x in self.allowed_containers}) != len(self.allowed_containers)
        ):
            raise ValueError("INVALID_HOST_CONTAINMENT")
        return self
