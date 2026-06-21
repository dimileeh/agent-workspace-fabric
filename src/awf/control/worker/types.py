"""Worker internal result types."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from awf.db.enums import WorkspaceStatus
from awf.service.scheduler import SchedulerOrderCursor


class _ExecutionTaskKind(StrEnum):
    """Kind of work tracked in ``ControlWorker._execution_tasks`` slot accounting."""

    MONITOR_RESUME = "monitor_resume"
    READY = "ready"
    PRESERVED_ACTIVE = "preserved_active"
    # A resume of a pre-PR ``blocked`` workspace after an operator decision.
    BLOCKED_RESUME = "blocked_resume"
    # A resume of a ``recovering`` workspace after the provider cooldown elapsed
    # (auto-healing in-place provider retry, #612).
    RECOVERING_RESUME = "recovering_resume"
    # A monitor resume that reconcile has cancelled but whose coroutine has not
    # yet stopped. Cancellation is cooperative, so the task can keep running
    # after ``cancel()`` returns. We keep it tracked under its workspace_id so a
    # fresh dispatch for the *same* workspace stays blocked, but exclude it from
    # the slot budget so it does not starve *other* workspaces.
    MONITOR_DRAINING = "monitor_draining"


type _AllocatedReservationSignature = tuple[int, int, int, int, int, int, int]

type _RequestedCapacityQueueSignature = tuple[
    int,
    datetime | None,
    datetime | None,
    str | None,
    str,
]


@dataclass(frozen=True)
class _RequestedCapacityClaimResult:
    workspace_ids: list[str]
    resume_after: SchedulerOrderCursor | None = None
    allocated_signature: _AllocatedReservationSignature | None = None
    requested_queue_signature: _RequestedCapacityQueueSignature | None = None
    provider_suppression_resume_expires_at: datetime | None = None


@dataclass(frozen=True)
class _SchedulerCandidateFilterResult:
    workspace_ids: list[str]
    provider_suppression_resume_expires_at: datetime | None = None


@dataclass(frozen=True)
class _ActiveExecutionCandidate:
    workspace_id: str
    status: WorkspaceStatus
    compose_project_name: str | None
    agent: str | None = None
    repo_url: str | None = None
    compose_file_path: str | None = None
    pr_url: str | None = None
    task_policy: dict[str, Any] | None = None


@dataclass(frozen=True)
class _TerminalRuntimeCandidate:
    workspace_id: str
    status: WorkspaceStatus
    # Legacy/partially persisted rows may have null compose_project_name; the
    # cleaner derives ``awf_<workspace_id>`` as the default. compose_file_path
    # can carry the runtime signal when project name was never persisted.
    compose_project_name: str | None
    compose_file_path: str | None
    repo_url: str
    # ``event_order`` of the ``terminal_runtime_released`` event the candidate was listed
    # under (the current effective-release cycle's floor). Carried only for deferred
    # overlay-umount candidates so the marker-write guards can verify, under the row lock,
    # that the latest release cycle is still the one that was retried before recording a
    # terminal/pending marker. ``None`` for candidates produced by other paths.
    release_cycle_floor: int | None = None


@dataclass(frozen=True)
class _PreservedWorktreeClassification:
    state: str
    reason: str
    worktree_path: str | None = None
    branch_name: str | None = None
    expected_branch_name: str | None = None
    base_commit: str | None = None
    head_sha: str | None = None
    commit_count: int | None = None
    status_porcelain: str | None = None
    error: str | None = None

    def to_payload(self: Any) -> dict[str, Any]:
        return {
            "state": self.state,
            "reason": self.reason,
            "worktree_path": self.worktree_path,
            "branch_name": self.branch_name,
            "expected_branch_name": self.expected_branch_name,
            "base_commit": self.base_commit,
            "head_sha": self.head_sha,
            "commit_count": self.commit_count,
            "status_porcelain": self.status_porcelain,
            "error": self.error,
        }


@dataclass(frozen=True)
class _OpenPullRequestSummary:
    pr_url: str
    pr_number: int
    head_ref: str | None
    head_sha: str | None
    head_repo_slug: str | None = None

    def to_payload(self: Any) -> dict[str, Any]:
        payload = {
            "pr_url": self.pr_url,
            "pr_number": self.pr_number,
            "head_ref": self.head_ref,
            "head_sha": self.head_sha,
        }
        if self.head_repo_slug is not None:
            payload["head_repo_slug"] = self.head_repo_slug
        return payload


@dataclass(frozen=True)
class _BranchOpenPRLookup:
    branch_name: str
    state: str
    payload: dict[str, Any]
    match: _OpenPullRequestSummary | None = None
    ambiguity_reason: str | None = None
