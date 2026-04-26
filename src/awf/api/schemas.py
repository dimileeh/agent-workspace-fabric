"""Pydantic schemas for the public API.

These schemas define the HTTP contract and are reused by the MCP server so
REST + MCP stay in lockstep. Keep them narrow: business objects live in
``awf.db.models``, not here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

from awf.db.enums import AgentRuntime, TaskClass, WorkspaceStatus
from awf.profiles.models import WorkspaceProfile

OwnedPath = Annotated[str, Field(min_length=1, max_length=512)]


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


class WorkspaceV2Repo(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    url: Annotated[str, Field(min_length=1, max_length=512)]
    base_branch: Annotated[str, Field(default="main", min_length=1, max_length=256)]


class WorkspaceV2Task(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: Annotated[str, Field(min_length=1, max_length=512)]
    prompt: Annotated[str, Field(min_length=1, max_length=16384)]
    kind: Annotated[str, Field(default="feature_branch_pr", max_length=32)]
    agent: AgentRuntime = Field(default=AgentRuntime.codex)
    external_id: Annotated[str | None, Field(default=None, max_length=128)]
    task_class: TaskClass | None = None
    owned_paths: list[OwnedPath] = Field(default_factory=list, max_length=128)
    auto_merge: bool = True
    initial_review_grace_period_seconds: float | None = Field(
        default=None,
        ge=0,
        le=86400,
    )


class WorkspaceV2Workspace(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    profile_ref: Annotated[str | None, Field(default="auto", max_length=128)]
    profile: WorkspaceProfile | None = None


class WorkspaceV2Validation(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    commands: list[str] = Field(default_factory=list)
    requested_tier: int = Field(default=1, ge=1, le=3)


class WorkspaceV2Resources(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    cpu: float | None = Field(default=None, gt=0)
    memory: Annotated[str | None, Field(default=None, max_length=32)]


class WorkspaceCreateV2Request(BaseModel):
    """Clean v2 workspace creation contract."""

    model_config = ConfigDict(extra="forbid")

    repo: WorkspaceV2Repo
    task: WorkspaceV2Task
    workspace: WorkspaceV2Workspace = Field(
        default_factory=lambda: WorkspaceV2Workspace(profile_ref="auto", profile=None)
    )
    validation: WorkspaceV2Validation = Field(default_factory=lambda: WorkspaceV2Validation())
    resources: WorkspaceV2Resources = Field(
        default_factory=lambda: WorkspaceV2Resources(cpu=None, memory=None)
    )


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
    task_class: TaskClass | None
    owned_paths: list[str]
    auto_merge: bool
    initial_review_grace_period_seconds: float | None

    agent: AgentRuntime
    env_profile: str | None
    profile_ref: str | None
    requested_profile: dict[str, Any] | None
    resolved_profile: dict[str, Any] | None

    test_commands: list[str]
    requires_database: bool

    node_id: str | None
    compose_project_name: str | None
    compose_file_path: str | None

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


class WorkspaceEventResponse(BaseModel):
    """Representation of an immutable workspace event."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    workspace_id: str
    event_type: str
    old_state: str | None
    new_state: str | None
    reason_code: str | None
    payload: dict[str, Any] | None
    occurred_at: datetime


class WorkspaceEventListResponse(BaseModel):
    """List envelope reserved for cursor pagination in a later slice."""

    items: list[WorkspaceEventResponse]
    next_cursor: str | None = None
    has_more: bool = False


class TaskResponse(BaseModel):
    """Workspace-backed task row for operator consoles."""

    task_id: str
    attempt_id: str | None = None
    attempt_number: int | None = None
    workspace_id: str
    title: str
    repo_url: str
    base_branch: str
    task_class: TaskClass | None
    owned_paths: list[str]
    agent: AgentRuntime
    status: WorkspaceStatus
    pr_url: str | None
    failure_reason: str | None
    created_at: datetime
    updated_at: datetime


class TaskListResponse(BaseModel):
    items: list[TaskResponse]
    next_cursor: str | None = None
    has_more: bool = False


class WorkspaceOverviewResponse(BaseModel):
    workspace_id: str
    task_id: str
    title: str
    repo_url: str
    base_branch: str
    branch_name: str | None
    task_class: TaskClass | None
    owned_paths: list[str]
    agent: AgentRuntime
    status: WorkspaceStatus
    current_phase: str
    active_operation: str | None
    last_event: WorkspaceEventResponse | None
    pr_url: str | None
    failure_reason: str | None
    failure_message: str | None
    created_at: datetime
    updated_at: datetime


class WorkspaceOverviewListResponse(BaseModel):
    items: list[WorkspaceOverviewResponse]
    next_cursor: str | None = None
    has_more: bool = False


class RuntimeServiceResponse(BaseModel):
    name: str
    container_id: str | None = None
    image: str | None = None
    state: str
    status: str | None = None
    health: str | None = None
    ports: list[str] = Field(default_factory=list)
    started_at: str | None = None


class WorkspaceRuntimeResponse(BaseModel):
    workspace_id: str
    compose_project_name: str | None
    stack_state: str
    services: list[RuntimeServiceResponse] = Field(default_factory=list)
    logs_available: bool
    control_available: bool
    reason: str | None = None


class WorkspaceLogStreamResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    stream_id: str
    source: str
    name: str
    kind: str
    path: str
    byte_count: int
    line_count: int
    opened_at: datetime
    closed_at: datetime | None


class WorkspaceLogListResponse(BaseModel):
    items: list[WorkspaceLogStreamResponse]
    next_cursor: str | None = None
    has_more: bool = False


class WorkspaceLogReadResponse(BaseModel):
    stream_id: str
    offset: int
    next_offset: int
    eof: bool
    data: str


class OperationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    workspace_id: str
    type: str
    status: str
    error_code: str | None
    error_message: str | None
    payload: dict[str, Any] | None
    result: dict[str, Any] | None
    idempotency_key: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class OperationListResponse(BaseModel):
    items: list[OperationResponse]
    next_cursor: str | None = None
    has_more: bool = False


class WorkspaceControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: Annotated[str | None, Field(default=None, max_length=1024)]
    stop_stack: bool = True


class WorkspaceControlResponse(BaseModel):
    workspace_id: str
    operation_id: str
    status: WorkspaceStatus
    message: str


class ErrorResponse(BaseModel):
    """Uniform error envelope.

    See docs/PLAN_MVP.md § Error code taxonomy for the canonical error codes.
    """

    error_code: str
    message: str
    detail: dict[str, Any] | None = None
