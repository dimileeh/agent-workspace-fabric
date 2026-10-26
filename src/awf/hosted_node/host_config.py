"""Server-owned Linux host observation inputs, never request configuration."""

from pathlib import Path
from typing import Annotated
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from awf.hosted_node.schemas import Opaque, StrictModel


class ControlContainer(StrictModel):
    id: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    image: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]


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
