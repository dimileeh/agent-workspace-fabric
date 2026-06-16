"""SQLAlchemy ORM models for the AWF control plane.

Three core tables for the MVP:

- ``workspaces``        : one row per workspace (isolated execution environment).
- ``operations``        : one row per async action (create, start, validate, cancel, destroy).
- ``workspace_events``  : append-only audit log of state transitions and notable events.

Design notes:

- ``Workspace.version`` enables optimistic concurrency for workspace mutations.
- ``Workspace.event_sequence`` is the workspace-local append-only event counter.
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
from sqlalchemy import (
    inspect as sa_inspect,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from awf.db.base import Base, _now

ADVISORY_PLAN_ARTIFACT_OVERLAP_REASON = "ADVISORY_PLAN_ARTIFACT_OVERLAP"
_ADVISORY_STALE_REASON_CODES = frozenset({ADVISORY_PLAN_ARTIFACT_OVERLAP_REASON})


def stale_reason_blocks_merge(reason_code: str | None) -> bool:
    """Return whether a stale reason should block merge/recovery.

    Unknown non-empty codes default to blocking for backward compatibility with
    existing rows and older call sites.
    """
    return reason_code is not None and reason_code not in _ADVISORY_STALE_REASON_CODES


def stale_reason_severity(reason_code: str | None) -> str:
    return "blocking" if stale_reason_blocks_merge(reason_code) else "advisory"


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
    subphase: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_activity_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_log_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    event_sequence: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )

    # Request inputs
    repo_url: Mapped[str] = mapped_column(String(512), nullable=False)
    branch_base: Mapped[str] = mapped_column(String(256), nullable=False)
    branch_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    """Local branch in the worktree — what the agent commits to."""

    task_tag: Mapped[str | None] = mapped_column(String(64), nullable=True)
    """Optional Jira-style issue key (e.g. ``PROJ-123``) prepended to the
    branch name, PR title, and every AWF-authored commit message so work
    auto-links to the issue in Jira/BitBucket. Nullable; absent = no-op."""

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

    task_policy: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'")
    )
    """Task-scoped policy metadata supplied by the planner/request."""

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
    """Requested workspace profile reference (``auto``, ``python``,
    ``docker-compose``, ``aira``, etc.). Nullable for legacy rows."""

    requested_profile: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    """Inline profile supplied by the caller. Stored separately from the
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

    execution_claim_epoch: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
    """Monotonic fencing token for the active-execution claim.

    Unlike ``version`` (which bumps on every status transition and would
    spuriously fence a live worker), this advances only when the claim changes
    hands — on claim (``requested -> provisioning``) and at every recovery site
    that clears the claim. A later claimant always holds a strictly higher
    epoch, so a stale worker's owner-keyed heartbeat/release/terminal CAS
    updates 0 rows and the worker aborts before any destructive filesystem op.
    Backfilled to ``0`` for pre-migration rows; the first lease action bumps an
    in-flight row to ``1``."""

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

    # Block-state metadata (populated only while status == ``blocked``).
    block_reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    """Reason code for the current pause (e.g. ``QUALITY_GATE_POLICY_CHANGED``)."""

    block_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    """Block category (e.g. ``protected_quality_gate``)."""

    block_violations: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    """Normalized protected-violation records: ``[{path, section, line, reason}]``."""

    block_resume_phase: Mapped[str | None] = mapped_column(String(64), nullable=True)
    """Executor phase the workspace was in when it blocked (audit/resume context)."""

    block_epoch: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
    """Monotonic counter incremented on EACH entry into ``blocked``.

    Operator grants are scoped to the ``block_epoch`` live at grant time; a
    re-block bumps this value, which invalidates all prior grants. This is the
    mechanism that makes "a later DIFFERENT change to the same file re-block":
    the stale grant no longer matches the current epoch. Distinct from
    ``execution_claim_epoch`` (claim fencing) — do not conflate the two."""

    blocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    """Wall-clock entry into the current block, for block-age surfacing."""

    pending_operator_hint: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    """Serialized pre-PR ``OperatorHint`` awaiting the worker resume path.

    Stored in a dedicated column rather than reusing ``monitor_threads_addressed``
    (which carries monitor-state semantics and is reset by monitor logic)."""

    # Idempotency
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # Terminal-runtime-release retry rotation marker (see ControlWorker)
    terminal_release_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    """Bumped by the terminal-runtime release sweep on every retry of a failing
    workspace so the candidate scan can rotate past stuck rows. Kept separate
    from ``updated_at`` because retention paths (``service/gc.py`` cutoff and
    ``service/orphan_resources.py`` retained-id calculation) use
    ``updated_at`` as the age cutoff, and bumping it on every retry would
    indefinitely defer cleanup of persistently-failing workspaces."""

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
    egress_audit_records: Mapped[list[EgressAuditRecord]] = relationship(
        back_populates="workspace",
        cascade="all, delete-orphan",
        lazy="select",
        order_by="EgressAuditRecord.enforced_at",
    )
    operator_granted_paths: Mapped[list[OperatorGrantAuditRecord]] = relationship(
        back_populates="workspace",
        cascade="all, delete-orphan",
        lazy="select",
        order_by="OperatorGrantAuditRecord.created_at",
    )
    task_attempt: Mapped[TaskAttempt | None] = relationship(
        back_populates="workspace",
        cascade="all, delete-orphan",
        lazy="selectin",
        uselist=False,
    )
    queue_decisions: Mapped[list[QueueDecision]] = relationship(
        back_populates="workspace",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="QueueDecision.decided_at",
    )
    resource_reservations: Mapped[list[ResourceReservation]] = relationship(
        back_populates="workspace",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="ResourceReservation.reserved_at",
    )
    merge_candidates: Mapped[list[MergeCandidate]] = relationship(
        back_populates="workspace",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="MergeCandidate.updated_at",
    )
    policy_findings: Mapped[list[PolicyFinding]] = relationship(
        back_populates="workspace",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="PolicyFinding.detected_at",
    )
    validation_runs: Mapped[list[ValidationRun]] = relationship(
        back_populates="workspace",
        cascade="all, delete-orphan",
        lazy="raise",
        order_by="ValidationRun.started_at",
    )
    secret_leases: Mapped[list[WorkspaceSecretLease]] = relationship(
        back_populates="workspace",
        cascade="all, delete-orphan",
        lazy="raise",
        order_by="WorkspaceSecretLease.issued_at",
    )

    @property
    def latest_queue_decision(self) -> dict[str, Any] | None:
        if "queue_decisions" in sa_inspect(self).unloaded:
            return None
        if not self.queue_decisions:
            return None
        latest = max(self.queue_decisions, key=lambda item: (item.decided_at, item.id))
        return _queue_decision_summary(latest)

    @property
    def active_resource_reservation(self) -> dict[str, Any] | None:
        if "resource_reservations" in sa_inspect(self).unloaded:
            return None
        active = [
            reservation
            for reservation in self.resource_reservations
            if reservation.released_at is None
        ]
        if not active:
            return None
        latest = max(active, key=lambda item: (item.reserved_at, item.id))
        return _resource_reservation_summary(latest)

    @property
    def active_policy_findings(self) -> list[PolicyFinding]:
        if "policy_findings" in sa_inspect(self).unloaded:
            return []
        return [finding for finding in self.policy_findings if finding.status == "active"]


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
    queue_decisions: Mapped[list[QueueDecision]] = relationship(
        back_populates="attempt",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="QueueDecision.decided_at",
    )
    resource_reservations: Mapped[list[ResourceReservation]] = relationship(
        back_populates="attempt",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="ResourceReservation.reserved_at",
    )
    validation_runs: Mapped[list[ValidationRun]] = relationship(
        back_populates="attempt",
        lazy="raise",
        order_by="ValidationRun.started_at",
    )
    secret_leases: Mapped[list[WorkspaceSecretLease]] = relationship(
        back_populates="attempt",
        lazy="raise",
        order_by="WorkspaceSecretLease.issued_at",
    )


class ProviderModelCircuitBreaker(Base):
    """Durable cooldown state for one provider/model pair."""

    __tablename__ = "provider_model_circuit_breakers"
    __table_args__ = (
        UniqueConstraint("provider", "model", name="uq_provider_model_circuit_breakers_pair"),
        Index("ix_provider_model_circuit_breakers_state", "state", "cooldown_until"),
        Index("ix_provider_model_circuit_breakers_provider_model", "provider", "model"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    provider: Mapped[str] = mapped_column(String(128), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="closed")
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_failure_fingerprint: Mapped[str | None] = mapped_column(String(512), nullable=True)
    last_workspace_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    last_attempt_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )


class WorkspaceSecretLease(Base):
    """Local control-plane metadata for a profile-declared workspace secret lease."""

    __tablename__ = "workspace_secret_leases"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "secret_name",
            "kind",
            "target",
            name="uq_workspace_secret_leases_declaration",
        ),
        Index("ix_workspace_secret_leases_workspace_status", "workspace_id", "status"),
        Index("ix_workspace_secret_leases_status_expires", "status", "expires_at"),
        Index("ix_workspace_secret_leases_workspace_issued", "workspace_id", "issued_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workspaces.id"), nullable=False
    )
    attempt_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("task_attempts.id"), nullable=True
    )

    secret_name: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    target: Mapped[str] = mapped_column(String(512), nullable=False)
    mode: Mapped[str] = mapped_column(String(8), nullable=False)
    required: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=true(),
    )
    provider: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ref_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)

    status: Mapped[str] = mapped_column(String(16), nullable=False, default="issued")
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    mounted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    issue_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'")
    )
    mount_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'")
    )
    revoke_reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    workspace: Mapped[Workspace] = relationship(back_populates="secret_leases")
    attempt: Mapped[TaskAttempt | None] = relationship(back_populates="secret_leases")


class QueueDecision(Base):
    """Durable scheduler admission and ordering decision for one attempt."""

    __tablename__ = "queue_decisions"
    __table_args__ = (
        Index("ix_queue_decisions_workspace", "workspace_id"),
        Index("ix_queue_decisions_attempt", "attempt_id"),
        Index("ix_queue_decisions_task", "task_id"),
        Index("ix_queue_decisions_decision", "decision"),
        Index("ix_queue_decisions_decided_at", "decided_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workspaces.id"), nullable=False
    )
    attempt_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("task_attempts.id"), nullable=False
    )
    task_id: Mapped[str] = mapped_column(String(36), ForeignKey("tasks.id"), nullable=False)

    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    class_priority: Mapped[int] = mapped_column(Integer, nullable=False)
    computed_priority: Mapped[int] = mapped_column(Integer, nullable=False)
    age_boost: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retry_bonus: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    resource_summary: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'")
    )
    overlap_risk_summary: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'")
    )
    score_summary: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'")
    )
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )

    workspace: Mapped[Workspace] = relationship(back_populates="queue_decisions")
    attempt: Mapped[TaskAttempt] = relationship(back_populates="queue_decisions")
    task: Mapped[Task] = relationship()


class ResourceReservation(Base):
    """Local node resource reservation for one workspace attempt."""

    __tablename__ = "resource_reservations"
    __table_args__ = (
        Index("ix_resource_reservations_workspace", "workspace_id"),
        Index("ix_resource_reservations_attempt", "attempt_id"),
        Index("ix_resource_reservations_node_active", "node_id", "released_at"),
        Index("ix_resource_reservations_reserved_at", "reserved_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workspaces.id"), nullable=False
    )
    attempt_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("task_attempts.id"), nullable=False
    )

    node_id: Mapped[str] = mapped_column(String(64), nullable=False)
    steady_cpu: Mapped[float] = mapped_column(Float, nullable=False)
    steady_memory_gb: Mapped[float] = mapped_column(Float, nullable=False)
    peak_cpu: Mapped[float] = mapped_column(Float, nullable=False)
    peak_memory_gb: Mapped[float] = mapped_column(Float, nullable=False)
    disk_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dind_slots: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    phase: Mapped[str] = mapped_column(String(32), nullable=False)
    reserved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    workspace: Mapped[Workspace] = relationship(back_populates="resource_reservations")
    attempt: Mapped[TaskAttempt] = relationship(back_populates="resource_reservations")


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
    policy_blocked: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=false(),
    )
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
    policy_findings: Mapped[list[PolicyFinding]] = relationship(
        back_populates="candidate",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="PolicyFinding.detected_at",
    )


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
    base_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    workspace_head_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_branch: Mapped[str | None] = mapped_column(String(256), nullable=True)
    target_head_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    profile_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    profile_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    profile_source: Mapped[str | None] = mapped_column(String(256), nullable=True)
    resolved_profile_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    environment_identity_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    environment_identity_inputs: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    status: Mapped[str] = mapped_column(String(16), nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    log_stream_refs: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'")
    )
    coverage: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

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
        Index("ix_operations_idempotency_key_created_at_id", "idempotency_key", "created_at", "id"),
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
        Index(
            "ix_workspace_events_workspace_occurred_order",
            "workspace_id",
            "occurred_at",
            "event_order",
        ),
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
    event_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )

    workspace: Mapped[Workspace] = relationship(back_populates="events")


class PRFeedbackResolution(Base):
    """PR-scoped monitor verdict for a review comment or thread.

    The identity is deliberately PR/provider feedback identity, not workspace
    identity. Workspaces fail and get replaced; the PR review state they already
    inspected must survive that replacement.
    """

    __tablename__ = "pr_feedback_resolutions"
    __table_args__ = (
        Index(
            "ix_pr_feedback_resolutions_pr",
            "scm_provider",
            "repository_key",
            "pull_request_key",
        ),
        Index("ix_pr_feedback_resolutions_head_sha", "head_sha"),
        Index("ix_pr_feedback_resolutions_source_workspace", "source_workspace_id"),
        Index("ix_pr_feedback_resolutions_updated_at", "updated_at"),
    )

    scm_provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    repository_key: Mapped[str] = mapped_column(String(512), primary_key=True)
    pull_request_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    head_sha: Mapped[str] = mapped_column(String(128), nullable=False)
    feedback_kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    feedback_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    feedback_body_hash: Mapped[str] = mapped_column(String(64), primary_key=True)

    pull_request_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    feedback_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    feedback_author: Mapped[str | None] = mapped_column(String(256), nullable=True)
    verdict: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    source_workspace_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    source_operation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    resolved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )


class CallbackSubscription(Base):
    """External operator callback target registration.

    This deliberately stores only the destination URL and event routing policy.
    User-supplied secrets, bearer tokens, and arbitrary outbound headers are not
    part of this local foundation.
    """

    __tablename__ = "callback_subscriptions"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_callback_subscriptions_idempotency_key"),
        Index("ix_callback_subscriptions_enabled_created", "enabled", "created_at"),
        Index("ix_callback_subscriptions_created_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    target_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    event_types: Mapped[list[str]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        server_default=text("'[]'"),
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=true(),
    )
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    initial_backoff_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    deliveries: Mapped[list[CallbackDelivery]] = relationship(
        back_populates="subscription",
        cascade="all, delete-orphan",
        lazy="raise",
        order_by="CallbackDelivery.created_at",
    )


class CallbackDelivery(Base):
    """Durable outbound callback delivery attempt state."""

    __tablename__ = "callback_deliveries"
    __table_args__ = (
        UniqueConstraint("subscription_id", "dedupe_key", name="uq_callback_deliveries_dedupe"),
        Index("ix_callback_deliveries_subscription", "subscription_id"),
        Index("ix_callback_deliveries_due", "status", "next_attempt_at", "created_at"),
        Index("ix_callback_deliveries_source", "event_kind", "source_id"),
        Index("ix_callback_deliveries_workspace", "workspace_id"),
        Index("ix_callback_deliveries_operation", "operation_id"),
        Index("ix_callback_deliveries_merge_candidate", "merge_candidate_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    subscription_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("callback_subscriptions.id"),
        nullable=False,
    )
    event_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(256), nullable=False)
    workspace_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("workspaces.id"),
        nullable=True,
    )
    operation_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("operations.id"),
        nullable=True,
    )
    merge_candidate_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("merge_candidates.id"),
        nullable=True,
    )
    envelope: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
        server_default=text("'{}'"),
    )
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    response_status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    subscription: Mapped[CallbackSubscription] = relationship(back_populates="deliveries")


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

    @property
    def blocks_merge(self) -> bool:
        return stale_reason_blocks_merge(self.reason_code)

    @property
    def severity(self) -> str:
        return stale_reason_severity(self.reason_code)


class PolicyFinding(Base):
    """One structured policy finding against a workspace or merge candidate."""

    __tablename__ = "policy_findings"
    __table_args__ = (
        Index("ix_policy_findings_workspace", "workspace_id"),
        Index("ix_policy_findings_candidate", "candidate_id"),
        Index("ix_policy_findings_status", "status"),
        Index("ix_policy_findings_severity", "severity"),
        Index("ix_policy_findings_detected_at", "detected_at"),
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

    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    subject_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    explanation: Mapped[str] = mapped_column(String(2048), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'")
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    workspace: Mapped[Workspace] = relationship(back_populates="policy_findings")
    candidate: Mapped[MergeCandidate | None] = relationship(back_populates="policy_findings")


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


class EgressAuditRecord(Base):
    """Immutable evidence of a workspace egress policy enforcement decision."""

    __tablename__ = "egress_audit_records"
    __table_args__ = (
        Index("ix_egress_audit_records_workspace", "workspace_id"),
        Index("ix_egress_audit_records_attempt", "attempt_id"),
        Index("ix_egress_audit_records_enforced_at", "enforced_at"),
        Index("ix_egress_audit_records_posture", "policy_posture"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workspaces.id"), nullable=False
    )
    attempt_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("task_attempts.id"), nullable=True
    )

    policy_posture: Mapped[str] = mapped_column(String(16), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    destination_category: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'")
    )
    enforced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )

    workspace: Mapped[Workspace] = relationship(back_populates="egress_audit_records")


class OperatorGrantAuditRecord(Base):
    """Immutable audit record of an operator's scoped protected-path grant.

    An operator resolves a pre-PR ``blocked`` workspace (protected quality-gate
    violation) by APPROVE-AND-KEEP: a scoped, single-use, epoch-bound grant for a
    specific repo-relative path glob. This is deliberately NOT a flat standing
    path list on the workspace — each grant authorizes exactly one block instance
    (``block_epoch``) and is consumed once applied, so a later DIFFERENT change to
    the same file (a new block, a bumped epoch) must be granted again. The agent
    can never write these rows; ``operator`` is always set by the control plane.
    """

    __tablename__ = "operator_grant_audit_records"
    __table_args__ = (
        Index("ix_operator_grant_audit_records_workspace", "workspace_id"),
        Index("ix_operator_grant_audit_records_created_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workspaces.id"), nullable=False
    )

    operator: Mapped[str] = mapped_column(String(256), nullable=False)
    """Identity of the operator who issued the grant (control-plane-set)."""

    reason: Mapped[str] = mapped_column(String(2048), nullable=False)
    """Required operator audit reason for the grant."""

    normalized_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    """Canonicalized, repo-relative path glob the grant authorizes (no ``../``)."""

    block_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    """The ``Workspace.block_epoch`` value this grant authorizes. A grant is only
    active while it equals the workspace's current epoch."""

    approve_policy_downgrade: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    """Operator acknowledgement that the granted edit may WEAKEN validation. A
    weakening edit (or a classifier-less protected file) requires this flag."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    """Set when the grant is applied past the gate (single-use)."""

    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    """Reserved for an explicit operator revoke (no CLI surface this slice)."""

    workspace: Mapped[Workspace] = relationship(back_populates="operator_granted_paths")

    @property
    def is_active_for_epoch(self) -> bool:
        """A grant authorizes the gate iff it is unconsumed, unrevoked, and
        scoped to the workspace's CURRENT block epoch.

        Epoch comparison is the caller's responsibility (it needs the live
        workspace value); this property only covers the consumed/revoked legs."""
        return self.consumed_at is None and self.revoked_at is None


class WorkerHeartbeat(Base):
    """Latest liveness heartbeat written by one control-worker process."""

    __tablename__ = "worker_heartbeats"
    __table_args__ = (
        Index("ix_worker_heartbeats_node_id", "node_id"),
        Index("ix_worker_heartbeats_last_heartbeat_at", "last_heartbeat_at"),
        Index("ix_worker_heartbeats_node_last_heartbeat", "node_id", "last_heartbeat_at"),
    )

    worker_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    node_id: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    poll_interval_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )


class ServiceGCRequest(Base):
    """On-demand request for the worker to run its capability-gated GC reap (#582).

    ``awf service gc --execute`` resolves in the API container, which lacks
    ``CAP_SYS_ADMIN`` and so cannot unmount/reclaim the per-workspace Claude auth
    overlays or ``_shared/claude-base``. Only the worker can. The API and worker
    are separate processes whose only shared channel is this control-plane DB, so
    the API inserts a ``pending`` row here and the worker — discovering it on its
    poll loop — claims it (``SELECT ... FOR UPDATE SKIP LOCKED``), runs the
    existing terminal-workspace + claude-base reap, and writes the reclaimed
    report back into ``result`` before marking it ``completed``. The API folds that
    report into the gc response so the operator sees real reclamation instead of
    ``deleted_path_count: 0``. Mirrors the system-scoped ``worker_heartbeats``
    table pattern (no ``workspace_id``, since gc is service-scoped).
    """

    __tablename__ = "service_gc_requests"
    __table_args__ = (
        Index("ix_service_gc_requests_status", "status"),
        Index("ix_service_gc_requests_requested_at", "requested_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    node_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )


def _queue_decision_summary(decision: QueueDecision) -> dict[str, Any]:
    return {
        "id": decision.id,
        "decision": decision.decision,
        "reason_code": decision.reason_code,
        "class_priority": decision.class_priority,
        "computed_priority": decision.computed_priority,
        "age_boost": decision.age_boost,
        "retry_bonus": decision.retry_bonus,
        "resource_summary": decision.resource_summary,
        "overlap_risk_summary": decision.overlap_risk_summary,
        "score_summary": decision.score_summary,
        "decided_at": decision.decided_at,
    }


def _resource_reservation_summary(reservation: ResourceReservation) -> dict[str, Any]:
    return {
        "id": reservation.id,
        "node_id": reservation.node_id,
        "steady_cpu": reservation.steady_cpu,
        "steady_memory_gb": reservation.steady_memory_gb,
        "peak_cpu": reservation.peak_cpu,
        "peak_memory_gb": reservation.peak_memory_gb,
        "disk_mb": reservation.disk_mb,
        "dind_slots": reservation.dind_slots,
        "phase": reservation.phase,
        "reserved_at": reservation.reserved_at,
        "released_at": reservation.released_at,
    }
