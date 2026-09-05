"""PR-monitor adoption *request* contracts.

Split out of :mod:`awf.api.schemas` to keep that module under the
maintainability line limit. These are the request-side models for
``POST /v1/workspaces/adopt-pr``; they are re-exported from ``awf.api.schemas``
so ``from awf.api.schemas import PullRequestMonitorAdoptionRequest`` keeps
working for the REST app, the CLI contract tests, and the MCP server.

``PullRequestMonitorAdoptionResponse`` deliberately stays in ``schemas`` because
it composes response leaves defined there.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from awf.api import schemas_operations as _schemas_operations
from awf.api.cursor_auto import CursorAutoModeSelectionMixin
from awf.common.external_id import validate_external_id
from awf.common.task_tag import validate_task_tag
from awf.db.enums import AgentRuntime, TaskClass
from awf.profiles.models import WorkspaceProfile

OwnedPath = _schemas_operations.OwnedPath

# Mirrors ``WorkspaceGuideRequest.directive``: the adoption ``hint`` is armed as
# the same kind of pending operator directive, so it carries the same bound.
ADOPTION_HINT_MAX_LENGTH = 1024


class PullRequestMonitorExecutionPolicy(BaseModel):
    """Execution placement policy for adopted PR monitor repair/validation."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    mode: Literal["local", "hosted"] = Field(
        default="local",
        description=(
            "Where Core should run PR-monitor repair and post-repair validation. "
            "'local' preserves Docker Compose execution; 'hosted' requires "
            "configured hosted delegation and never starts local Compose."
        ),
    )


class PullRequestMonitorAdoptionRequest(CursorAutoModeSelectionMixin):
    """Input for adopting an already-open GitHub PR into AWF monitoring."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    repo_url: Annotated[str | None, Field(default=None, min_length=1, max_length=512)] = None
    repo_slug: Annotated[str | None, Field(default=None, min_length=1, max_length=256)] = None
    pr_number: int | None = Field(default=None, ge=1)
    pr_url: Annotated[str | None, Field(default=None, min_length=1, max_length=512)] = None

    agent: AgentRuntime = Field(default=AgentRuntime.codex)
    model: Annotated[str | None, Field(default=None, min_length=1, max_length=128)] = None
    effort: Annotated[str | None, Field(default=None, min_length=1, max_length=64)] = None
    profile_ref: Annotated[str | None, Field(default="auto", max_length=128)] = "auto"
    profile: WorkspaceProfile | None = None
    owned_paths: list[OwnedPath] = Field(default_factory=list, max_length=128)
    auto_merge: bool | None = Field(
        default=None,
        description=(
            "Tri-state auto-merge intent for the adopted PR. True/False set it "
            "explicitly; omit (null) to fall through to the repo profile "
            "(monitor.auto_merge) and the uniform default (off). When resolved "
            "off the monitor reports readiness without merging (manual gate)."
        ),
    )
    execution: PullRequestMonitorExecutionPolicy = Field(
        default_factory=PullRequestMonitorExecutionPolicy,
        description=(
            "Explicit PR monitor execution policy. Hosted mode is never inferred "
            "from environment or Docker availability."
        ),
    )
    initial_review_grace_period_seconds: float | None = Field(default=None, ge=0, le=86400)
    task_title: Annotated[str | None, Field(default=None, min_length=1, max_length=512)] = None
    task_prompt: Annotated[str | None, Field(default=None, min_length=1, max_length=16384)] = None
    task_tag: Annotated[
        str | None,
        Field(
            default=None,
            max_length=64,
            description=(
                "Optional issue/task key linked into the PR title and "
                "AWF-authored monitor commits. Accepts a Jira issue key "
                "(PROJ-123) or an Aira task entity key (PROJ-T123). Pass bare "
                "keys; bracketed [PROJ-T123] is accepted and normalized, but "
                "bare is recommended because [ is a shell glob character."
            ),
        ),
    ] = None
    external_id: Annotated[
        str | None,
        Field(
            default=None,
            max_length=128,
            description=(
                "Optional external task id persisted on the adopted workspace and "
                "task for join/policy parity with workspace create. Omit to use the "
                "generated repo/PR adoption identity. Changing this on a live "
                "adoption returns PR_ADOPTION_POLICY_CONFLICT; an id owned by "
                "another task scope returns TASK_EXTERNAL_ID_CONFLICT."
            ),
        ),
    ] = None
    task_class: TaskClass | None = Field(
        default=None,
        description=(
            "Optional task class for scheduling and policy parity with workspace "
            "create. Omit to leave unset. Changing this on a live adoption returns "
            "PR_ADOPTION_POLICY_CONFLICT."
        ),
    )
    reason: Annotated[str | None, Field(default=None, max_length=512)] = None
    hint: Annotated[
        str | None,
        Field(
            default=None,
            max_length=ADOPTION_HINT_MAX_LENGTH,
            description=(
                "Optional operator directive armed on the adopted workspace as a "
                "pending operator hint, so the monitor's first decision cycle "
                "addresses it before any PR review comments. Use it to steer the "
                "re-adoption (e.g. 'do not edit .github/workflows/*'). Ignored "
                "when the request attaches to an already-live adoption; use the "
                "guide control for that."
            ),
        ),
    ] = None

    @field_validator("task_tag")
    @classmethod
    def _validate_task_tag(cls, value: str | None) -> str | None:
        """Normalize and validate an optional task tag; ``None`` when absent."""
        return validate_task_tag(value)

    @field_validator("external_id")
    @classmethod
    def _validate_external_id(cls, value: str | None) -> str | None:
        """Reject ASCII controls so malformed ids fail as 422, not at DB flush."""
        return validate_external_id(value)

    @field_validator("hint")
    @classmethod
    def _normalize_hint(cls, value: str | None) -> str | None:
        """Collapse a blank directive to ``None``.

        ``str_strip_whitespace`` already trims, so a whitespace-only hint arrives
        as ``""``. Normalizing it away keeps a hollow hint from arming a pending
        ``AddressOperatorHint`` cycle with nothing for the agent to act on --
        the same invariant ``guide_workspace`` enforces with
        ``WorkspaceGuideEmptyDirectiveError``.
        """
        return value or None
