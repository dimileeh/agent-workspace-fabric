"""Pydantic schemas for the public API.

These schemas define the HTTP contract and are reused by the MCP server so
REST + MCP stay in lockstep. Keep them narrow: business objects live in
``awf.db.models``, not here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from awf.db.enums import AgentRuntime, WorkspaceStatus


class WorkspaceCreateRequest(BaseModel):
    """Input for ``POST /v1/workspaces`` and ``awf_create_workspace`` (MCP).

    Fields are grouped logically; see docs/PLAN_MVP.md § API surface.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    repo_url: Annotated[str, Field(min_length=1, max_length=512)]
    branch_base: Annotated[str, Field(default="development", min_length=1, max_length=256)]

    task_title: Annotated[str, Field(min_length=1, max_length=512)]
    task_prompt: Annotated[str, Field(min_length=1, max_length=16384)]
    task_external_id: Annotated[str | None, Field(default=None, max_length=128)]

    agent: AgentRuntime = Field(default=AgentRuntime.codex)
    env_profile: Annotated[str | None, Field(default=None, max_length=128)]

    test_commands: list[str] = Field(default_factory=list)
    requires_database: bool = False


class WorkspaceResponse(BaseModel):
    """Representation of a workspace in API responses."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    status: WorkspaceStatus
    version: int

    repo_url: str
    branch_base: str
    branch_name: str | None
    base_commit: str | None

    task_title: str
    task_prompt: str
    task_external_id: str | None

    agent: AgentRuntime
    env_profile: str | None

    test_commands: list[str]
    requires_database: bool

    node_id: str | None
    compose_project_name: str | None

    pr_url: str | None
    failure_reason: str | None
    failure_message: str | None

    created_at: datetime
    updated_at: datetime


class WorkspaceAcceptedResponse(BaseModel):
    """202 Accepted payload for workspace creation.

    Returned when provisioning hasn't completed within ``wait_timeout_seconds``.
    Clients poll ``status_url`` or subscribe to events.
    """

    workspace_id: str
    status: WorkspaceStatus
    version: int
    status_url: str
    events_url: str
    accepted_at: datetime


class ErrorResponse(BaseModel):
    """Uniform error envelope.

    See docs/PLAN_MVP.md § Error code taxonomy for the canonical error codes.
    """

    error_code: str
    message: str
    detail: dict[str, str] | None = None
