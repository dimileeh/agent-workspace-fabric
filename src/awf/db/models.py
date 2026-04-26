"""SQLAlchemy ORM models for the AWF control plane.

Three core tables for the MVP:

- ``workspaces``        : one row per workspace (isolated execution environment).
- ``operations``        : one row per async action (create, start, validate, cancel, destroy).
- ``workspace_events``  : append-only audit log of state transitions and notable events.

Design notes:

- ``Workspace.version`` enables optimistic concurrency: repositories bump + check it.
- ``Workspace.idempotency_key`` is unique (nullable) — duplicate POSTs with the same
  key return the existing workspace rather than creating a second one.
- Status columns are stored as strings, not DB-level enums, so migrations don't
  require DB-level enum alterations every time we add a state.
- ``WorkspaceEvent`` is append-only; no UPDATE/DELETE should ever touch it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    false,
    text,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from awf.db.base import Base, _now


class Workspace(Base):
    __tablename__ = "workspaces"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_workspaces_idempotency_key"),
        Index("ix_workspaces_status", "status"),
        Index("ix_workspaces_status_updated_at", "status", "updated_at"),
        Index("ix_workspaces_created_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # Request inputs
    repo_url: Mapped[str] = mapped_column(String(512), nullable=False)
    branch_base: Mapped[str] = mapped_column(String(256), nullable=False)
    branch_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    """Local branch in the worktree — what the agent commits to."""

    remote_push_branch: Mapped[str | None] = mapped_column(String(256), nullable=True)
    """Remote branch the monitor pushes to. For ``feature_branch_pr`` this
    equals ``branch_name`` (push feature branch back to its own remote
    ref). For ``sync_release_pr`` / ``sync_feature_pr`` it's the PR's
    head branch (``development``, the PR head, etc.) — the local branch
    name (``release-sync/<id>`` / ``feature-sync/<id>``) is a
    per-workspace ref for race avoidance and must NOT leak to origin.

    The monitor uses this with an explicit push refspec
    (``HEAD:refs/heads/<remote_push_branch>``) so push semantics don't
    depend on ``branch.<X>.merge`` / ``push.default`` git config, which
    have been observed leaking across worktrees via the shared bare
    mirror (see T39 incident 2026-04-23: four feature-branch commits
    pushed to ``development`` on aira-web because the monitor used
    ``git push origin HEAD`` with polluted config). Nullable for
    backward-compat with pre-migration rows; defaults to ``branch_name``
    at push time when unset."""

    base_commit: Mapped[str | None] = mapped_column(String(64), nullable=True)

    task_title: Mapped[str] = mapped_column(String(512), nullable=False)
    task_prompt: Mapped[str] = mapped_column(String(16384), nullable=False)
    task_external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    task_class: Mapped[str | None] = mapped_column(String(32), nullable=True)
    """Optional PRD policy class used by later deterministic scheduling work."""

    owned_paths: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'")
    )
    """Caller-declared path globs/strings the task expects to own.

    Owned paths are coordination hints and stale-detection inputs. They are not
    exclusive code locks, and overlapping owned paths do not block workspace
    admission by themselves.
    """

    auto_merge: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=true(),
    )
    """Whether the service PR monitor may merge the PR once gates are green.

    ``False`` routes feature workspaces through the release/manual monitor
    behavior: post the ready-for-human comment and keep polling until an
    external merge is observed.
    """

    initial_review_grace_period_seconds: Mapped[float | None] = mapped_column(
        Float, nullable=True, default=None
    )
    """Optional workspace-specific override for the profile monitor grace.

    ``None`` means use ``resolved_profile.monitor.initial_review_grace_period_seconds``.
    """

    agent: Mapped[str] = mapped_column(String(32), nullable=False)
    env_profile: Mapped[str | None] = mapped_column(String(128), nullable=True)
    profile_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    """Requested v2 workspace profile reference (``auto``, ``python``,
    ``docker-compose``, ``aira``, etc.). Nullable for legacy v1 rows."""

    requested_profile: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    """Inline v2 profile supplied by the caller. Stored separately from the
    immutable resolved snapshot so operators can see what was requested."""

    resolved_profile: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    """Immutable profile snapshot used for this workspace after repo-local
    config / registry / detector resolution. This makes runs reproducible even
    if a repo's ``.awf/workspace.yml`` changes later."""

    # Validation inputs (list of shell commands, stored as JSON for portability)
    test_commands: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    requires_database: Mapped[bool] = mapped_column(default=False, nullable=False)

    # Runtime placement + compose project
    node_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    compose_project_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    compose_file_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    execution_claimed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    """Ephemeral service-worker owner currently driving active execution."""

    execution_claim_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    """Lease expiry for ``execution_claimed_by`` so crashed workers can be recovered."""

    # Terminal-state metadata
    pr_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """Extracted from ``pr_url`` when the PR is opened; the monitor loop
    uses it for GraphQL queries + ``gh pr ...`` calls."""

    pr_merge_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    """The squash-merge commit SHA recorded when the monitor merges the PR.
    Empty for release-PR monitors (they never merge)."""

    failure_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_message: Mapped[str | None] = mapped_column(String(2048), nullable=True)

    # Task kind + PR-monitor state (populated only during monitoring_pr).
    task_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="feature_branch_pr")
    """One of ``TaskKind``. Defaults to ``feature_branch_pr`` — every
    existing row pre-migration is a feature PR task."""

    monitor_iter_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    """Counts non-passive monitor iterations (AddressComments / SyncBase /
    ReportCiFailure). Kept for structured-log context only — no budget
    gate fires on high counts. The monitor drives the PR to Merge /
    NotifyHuman regardless of how many iterations that takes."""

    monitor_threads_addressed: Mapped[dict[str, str]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    """Persisted ``MonitorState.threads_addressed_ids`` — map of
    thread/comment ID → verdict (fix_committed / false_positive / defer).
    Survives a mid-loop crash so the monitor doesn't re-address on resume."""

    monitor_last_commit_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    """HEAD SHA after the most recent monitor push. Compared against
    ``PRStatus.head_sha`` to detect "CLI said it fixed but didn't actually
    commit"."""

    monitor_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    """Wall-clock start of the monitor phase, for wall-clock-cap arithmetic."""

    monitor_claimed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    """Ephemeral service-worker owner currently resuming the PR monitor."""

    monitor_claim_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    """Lease expiry for ``monitor_claimed_by`` so crashed workers do not wedge recovery."""

    # Idempotency
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    # Relationships — eagerly loaded via ``selectin`` so async callers can access
    # ``ws.events`` / ``ws.operations`` without triggering lazy I/O in a non-async context.
    # Both collections are bounded (O(tens) per workspace in the MVP lifecycle) so the
    # extra SELECT is negligible.
    operations: Mapped[list[Operation]] = relationship(
        back_populates="workspace",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="Operation.created_at",
    )
    events: Mapped[list[WorkspaceEvent]] = relationship(
        back_populates="workspace",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="WorkspaceEvent.occurred_at",
    )
    log_streams: Mapped[list[WorkspaceLogStream]] = relationship(
        back_populates="workspace",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="WorkspaceLogStream.opened_at",
    )
    task_attempt: Mapped[TaskAttempt | None] = relationship(
        back_populates="workspace",
        cascade="all, delete-orphan",
        lazy="selectin",
        uselist=False,
    )
    merge_candidates: Mapped[list[MergeCandidate]] = relationship(
        back_populates="workspace",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="MergeCandidate.updated_at",
    )
    validation_runs: Mapped[list[ValidationRun]] = relationship(
        back_populates="workspace",
        cascade="all, delete-orphan",
        lazy="raise",
        order_by="ValidationRun.started_at",
    )


class Task(Base):
    """First-class logical task, separate from any workspace execution attempt."""

    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("external_id", name="uq_tasks_external_id"),
        UniqueConstraint("idempotency_key", name="uq_tasks_idempotency_key"),
        Index("ix_tasks_created_at", "created_at"),
        Index("ix_tasks_repo_base", "repo_url", "base_branch"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)

    repo_url: Mapped[str] = mapped_column(String(512), nullable=False)
    base_branch: Mapped[str] = mapped_column(String(256), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    prompt: Mapped[str] = mapped_column(String(16384), nullable=False)
    task_class: Mapped[str | None] = mapped_column(String(32), nullable=True)
    owned_paths: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'")
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    attempts: Mapped[list[TaskAttempt]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="TaskAttempt.attempt_number",
    )
    merge_candidates: Mapped[list[MergeCandidate]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="MergeCandidate.updated_at",
    )


class TaskAttempt(Base):
    """One execution lineage node for a task, currently backed by one workspace."""

    __tablename__ = "task_attempts"
    __table_args__ = (
        UniqueConstraint("workspace_id", name="uq_task_attempts_workspace_id"),
        UniqueConstraint("task_id", "attempt_number", name="uq_task_attempts_task_number"),
        Index(
            "uq_task_attempts_one_canonical_per_task",
            "task_id",
            unique=True,
            sqlite_where=text("is_canonical_for_merge = 1"),
            postgresql_where=text("is_canonical_for_merge = true"),
        ),
        Index("ix_task_attempts_task", "task_id"),
        Index("ix_task_attempts_status", "status"),
        Index("ix_task_attempts_created_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(36), ForeignKey("tasks.id"), nullable=False)
    workspace_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workspaces.id"), nullable=False
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_attempt_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("task_attempts.id"), nullable=True
    )
    redispatch_from_attempt_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("task_attempts.id"), nullable=True
    )
    superseded_by_attempt_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("task_attempts.id"), nullable=True
    )
    is_canonical_for_merge: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=false(),
    )

    agent: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)

    repo_url: Mapped[str] = mapped_column(String(512), nullable=False)
    base_branch: Mapped[str] = mapped_column(String(256), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    task_class: Mapped[str | None] = mapped_column(String(32), nullable=True)
    owned_paths: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'")
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    task: Mapped[Task] = relationship(back_populates="attempts")
    workspace: Mapped[Workspace] = relationship(back_populates="task_attempt")
    merge_candidate: Mapped[MergeCandidate | None] = relationship(
        back_populates="attempt",
        cascade="all, delete-orphan",
        lazy="selectin",
        uselist=False,
    )
    validation_runs: Mapped[list[ValidationRun]] = relationship(
        back_populates="attempt",
        lazy="raise",
        order_by="ValidationRun.started_at",
    )


class MergeCandidate(Base):
    """Explicit PR-backed merge queue entry for a task attempt."""

    __tablename__ = "merge_candidates"
    __table_args__ = (
        UniqueConstraint("attempt_id", name="uq_merge_candidates_attempt_id"),
        Index("ix_merge_candidates_task", "task_id"),
        Index("ix_merge_candidates_workspace", "workspace_id"),
        Index("ix_merge_candidates_status_updated", "status", "updated_at"),
        Index("ix_merge_candidates_repo_base", "repo_url", "base_branch"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(36), ForeignKey("tasks.id"), nullable=False)
    attempt_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("task_attempts.id"), nullable=False
    )
    workspace_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workspaces.id"), nullable=False
    )

    pr_url: Mapped[str] = mapped_column(String(512), nullable=False)
    pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    repo_url: Mapped[str] = mapped_column(String(512), nullable=False)
    base_branch: Mapped[str] = mapped_column(String(256), nullable=False)
    branch_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    head_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    base_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)

    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    close_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)

    ready: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    manual_merge_required: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )
    waiting_for_monitor: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    failed_or_cancelled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    completed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    not_canonical: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    stale: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    stale_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    merged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    task: Mapped[Task] = relationship(back_populates="merge_candidates")
    attempt: Mapped[TaskAttempt] = relationship(back_populates="merge_candidate")
    workspace: Mapped[Workspace] = relationship(back_populates="merge_candidates")


class ValidationRun(Base):
    """Durable validation provenance for one configured command execution pass."""

    __tablename__ = "validation_runs"
    __table_args__ = (
        Index("ix_validation_runs_workspace_started", "workspace_id", "started_at"),
        Index("ix_validation_runs_workspace_finished", "workspace_id", "finished_at"),
        Index("ix_validation_runs_attempt", "attempt_id"),
        Index("ix_validation_runs_status", "status"),
        Index("ix_validation_runs_tier", "tier"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workspaces.id"), nullable=False
    )
    attempt_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("task_attempts.id"), nullable=True
    )

    tier: Mapped[int] = mapped_column(Integer, nullable=False)
    command_set_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    commands: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'")
    )

    base_commit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_branch: Mapped[str | None] = mapped_column(String(256), nullable=True)
    target_head_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)

    status: Mapped[str] = mapped_column(String(16), nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    log_stream_refs: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'")
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_now,
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_now,
        onupdate=_now,
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )

    workspace: Mapped[Workspace] = relationship(back_populates="validation_runs")
    attempt: Mapped[TaskAttempt | None] = relationship(back_populates="validation_runs")


class Operation(Base):
    __tablename__ = "operations"
    __table_args__ = (
        Index("ix_operations_workspace", "workspace_id"),
        Index("ix_operations_status", "status"),
        Index("ix_operations_type", "type"),
        Index("ix_operations_created_at_id", "created_at", "id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workspaces.id"), nullable=False
    )
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)

    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(2048), nullable=True)

    # Payload + result are free-form JSON so we don't need schema changes per op type.
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    workspace: Mapped[Workspace] = relationship(back_populates="operations")


class WorkspaceEvent(Base):
    """Append-only audit log. Records every state transition + notable event.

    No UPDATE/DELETE should ever hit this table — repositories enforce this by
    providing an ``add_event`` method and no ``update_event`` equivalent.
    """

    __tablename__ = "workspace_events"
    __table_args__ = (
        Index("ix_workspace_events_workspace", "workspace_id"),
        Index("ix_workspace_events_occurred_at", "occurred_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workspaces.id"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    old_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    new_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )

    workspace: Mapped[Workspace] = relationship(back_populates="events")


class StaleReason(Base):
    """One structured staleness finding against a candidate / workspace.

    Stale-detection produces these rows whenever the refresh service finds
    the candidate's validation base is no longer fresh against the target
    branch. ``status='active'`` rows drive the ``MergeCandidate.stale``
    flag and the merge-queue ``stale`` blocker reason; rows are flipped
    to ``status='resolved'`` (with ``resolved_at`` set) when a later
    refresh shows the trigger no longer applies — the historical record
    is preserved so console clients can show a timeline.
    """

    __tablename__ = "stale_reasons"
    __table_args__ = (
        Index("ix_stale_reasons_workspace", "workspace_id"),
        Index("ix_stale_reasons_candidate", "candidate_id"),
        Index("ix_stale_reasons_status", "status"),
        Index("ix_stale_reasons_detected_at", "detected_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workspaces.id"), nullable=False
    )
    candidate_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("merge_candidates.id"), nullable=True
    )
    attempt_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("task_attempts.id"), nullable=True
    )
    task_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("tasks.id"), nullable=True)

    trigger_type: Mapped[str] = mapped_column(String(64), nullable=False)
    """Coarse trigger taxonomy: ``target_advanced`` / ``path_overlap`` /
    ``schema_changed`` / ``dependency_changed`` / ``build_config_changed``.
    Used by clients that want to group reasons by class without parsing
    ``reason_code`` strings."""

    trigger_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    """Free-form reference for the trigger. For ``target_advanced`` this
    is the new target SHA; for path-based triggers it is the matching
    file path."""

    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    """One of ``STALE_TARGET_ADVANCED`` / ``STALE_OVERLAP`` /
    ``STALE_DEPENDENCY`` / ``STALE_BUILD_CONFIG`` / ``STALE_SCHEMA``."""

    explanation: Mapped[str] = mapped_column(String(2048), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    workspace: Mapped[Workspace] = relationship()
    candidate: Mapped[MergeCandidate | None] = relationship()


class WorkspaceLogStream(Base):
    """Durable index for one workspace log stream.

    The bytes live on disk so high-volume agent output does not bloat the
    control-plane database. This table stores enough metadata for console
    clients to list streams and read from stable byte offsets.
    """

    __tablename__ = "workspace_log_streams"
    __table_args__ = (
        UniqueConstraint("workspace_id", "stream_id", name="uq_workspace_log_stream"),
        Index("ix_workspace_log_streams_workspace", "workspace_id"),
        Index("ix_workspace_log_streams_opened_at", "opened_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workspaces.id"), nullable=False
    )
    stream_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    path: Mapped[str] = mapped_column(String(1024), nullable=False)
    byte_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    line_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    workspace: Mapped[Workspace] = relationship(back_populates="log_streams")
