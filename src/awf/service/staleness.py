"""Stale detection engine.

Two layers:

- ``evaluate_staleness`` is a pure function that turns a ``CandidateSnapshot`` +
  ``TargetBranchState`` + ``StalePolicy`` into a list of structured
  ``StalenessFinding`` records. No I/O, no SQLAlchemy, easily exercised
  from the unit tests.

- ``StalenessRefreshService`` wraps the pure function with persistence: it
  loads the merge candidate, asks an injected ``TargetBranchStateProvider``
  for the latest target snapshot (or accepts a literal one for tests),
  reconciles ``stale_reasons`` rows via ``StaleReasonRepository``, flips
  the ``MergeCandidate.stale`` boolean so the existing merge-queue
  reads keep working, and emits one ``workspace_events`` row per newly
  detected finding so console timelines surface the change without
  having to scrape logs.

Reason codes:

* ``STALE_TARGET_ADVANCED`` — target branch advanced past the candidate's
  validation base, and policy says freshness must be re-validated for
  this task class. Default-on for everything except ``docs_task`` /
  ``test_task`` with no overlapping or sensitive change.
* ``STALE_OVERLAP``         — owned-path overlap with a path changed on
  target. Always sensitive: even a docs-class candidate cannot ignore
  changes to a path it claims to own.
* ``STALE_SCHEMA``          — a schema / migration / model file changed
  on target while the candidate is a ``migration_task``.
* ``STALE_DEPENDENCY``      — a dependency file (``pyproject.toml`` etc.)
  changed while the candidate is a ``dependency_task`` (or migration_task,
  since dep changes can break a migration).
* ``STALE_BUILD_CONFIG``    — a build-config file (``Dockerfile``,
  ``docker-compose``, ``alembic.ini``) changed while the candidate is a
  ``build_config_task`` (or migration_task).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from awf.common.logging import get_logger
from awf.db.models import MergeCandidate, StaleReason, TaskAttempt, Workspace
from awf.db.repositories import (
    StaleReasonCreate,
    StaleReasonRepository,
    WorkspaceEventCreate,
    WorkspaceRepository,
)

_log = get_logger(__name__)


# ── Pure model ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CandidateSnapshot:
    """Candidate-side inputs the policy needs to evaluate staleness.

    Pulled out as its own type so the pure ``evaluate_staleness`` function
    can be unit-tested without a DB session and so the service layer can
    decide what to feed in (full ``MergeCandidate`` ORM rows, fixtures, etc.).
    """

    owned_paths: tuple[str, ...]
    task_class: str | None
    base_sha: str | None


@dataclass(frozen=True)
class TargetBranchState:
    """Snapshot of the target branch, captured at refresh time."""

    branch: str
    head_sha: str
    changed_paths: tuple[str, ...]
    """Paths that changed on the target branch since the candidate's
    ``base_sha`` (i.e. ``git diff --name-only <base_sha>..origin/<branch>``)."""

    advanced_commits: int
    """Commits the target moved beyond ``base_sha`` (``git rev-list --count``)."""


@dataclass(frozen=True)
class StalenessFinding:
    """One structured staleness signal produced by ``evaluate_staleness``."""

    reason_code: str
    trigger_type: str
    trigger_ref: str | None
    explanation: str


@dataclass(frozen=True)
class StalePolicy:
    """Path groups that drive the policy.

    Each entry is matched against changed paths with simple prefix /
    glob-prefix semantics — same comparator the workspace owned-path
    conflict check uses, so policy stays consistent with admission control.
    """

    schema_paths: tuple[str, ...]
    dependency_paths: tuple[str, ...]
    build_config_paths: tuple[str, ...]
    lenient_task_classes: tuple[str, ...] = field(
        default_factory=lambda: ("docs_task", "test_task")
    )


REASON_TARGET_ADVANCED: Final[str] = "STALE_TARGET_ADVANCED"
REASON_OVERLAP: Final[str] = "STALE_OVERLAP"
REASON_SCHEMA: Final[str] = "STALE_SCHEMA"
REASON_DEPENDENCY: Final[str] = "STALE_DEPENDENCY"
REASON_BUILD_CONFIG: Final[str] = "STALE_BUILD_CONFIG"

TRIGGER_TARGET_ADVANCED: Final[str] = "target_advanced"
TRIGGER_PATH_OVERLAP: Final[str] = "path_overlap"
TRIGGER_SCHEMA_CHANGED: Final[str] = "schema_changed"
TRIGGER_DEPENDENCY_CHANGED: Final[str] = "dependency_changed"
TRIGGER_BUILD_CONFIG_CHANGED: Final[str] = "build_config_changed"

DEFAULT_STALE_POLICY: Final[StalePolicy] = StalePolicy(
    schema_paths=(
        "migrations/",
        "src/awf/db/models.py",
        "src/awf/db/repositories.py",
        "alembic.ini",
    ),
    dependency_paths=(
        "pyproject.toml",
        "uv.lock",
        "requirements.txt",
        "package.json",
        "package-lock.json",
    ),
    build_config_paths=(
        "Dockerfile",
        "docker/",
        "docker-compose.yml",
        "docker-compose.yaml",
        ".github/workflows/",
    ),
)


# ── Pure logic ─────────────────────────────────────────────────────────────


def evaluate_staleness(
    *,
    candidate: CandidateSnapshot,
    target: TargetBranchState,
    policy: StalePolicy = DEFAULT_STALE_POLICY,
) -> list[StalenessFinding]:
    """Decide which staleness reasons apply to a candidate against ``target``.

    Returns an empty list when the candidate is fresh.
    """
    if candidate.base_sha is None:
        return []
    if target.head_sha == candidate.base_sha and target.advanced_commits == 0:
        return []

    findings: list[StalenessFinding] = []

    schema_changes = _matched(target.changed_paths, policy.schema_paths)
    dep_changes = _matched(target.changed_paths, policy.dependency_paths)
    build_changes = _matched(target.changed_paths, policy.build_config_paths)
    overlap = _matched(target.changed_paths, candidate.owned_paths)

    if candidate.task_class == "migration_task":
        for path in schema_changes:
            findings.append(
                StalenessFinding(
                    reason_code=REASON_SCHEMA,
                    trigger_type=TRIGGER_SCHEMA_CHANGED,
                    trigger_ref=path,
                    explanation=(
                        f"Schema/migration path '{path}' changed on target "
                        f"branch '{target.branch}'."
                    ),
                )
            )
            break

    if candidate.task_class in {"dependency_task", "migration_task"}:
        for path in dep_changes:
            findings.append(
                StalenessFinding(
                    reason_code=REASON_DEPENDENCY,
                    trigger_type=TRIGGER_DEPENDENCY_CHANGED,
                    trigger_ref=path,
                    explanation=(
                        f"Dependency manifest '{path}' changed on target branch '{target.branch}'."
                    ),
                )
            )
            break

    if candidate.task_class in {"build_config_task", "migration_task"}:
        for path in build_changes:
            findings.append(
                StalenessFinding(
                    reason_code=REASON_BUILD_CONFIG,
                    trigger_type=TRIGGER_BUILD_CONFIG_CHANGED,
                    trigger_ref=path,
                    explanation=(
                        f"Build config '{path}' changed on target branch '{target.branch}'."
                    ),
                )
            )
            break

    for path in overlap:
        findings.append(
            StalenessFinding(
                reason_code=REASON_OVERLAP,
                trigger_type=TRIGGER_PATH_OVERLAP,
                trigger_ref=path,
                explanation=(
                    f"Owned path '{path}' was changed on target branch '{target.branch}'."
                ),
            )
        )
        break

    if (
        not findings
        and target.advanced_commits > 0
        and candidate.task_class not in policy.lenient_task_classes
    ):
        findings.append(
            StalenessFinding(
                reason_code=REASON_TARGET_ADVANCED,
                trigger_type=TRIGGER_TARGET_ADVANCED,
                trigger_ref=target.head_sha,
                explanation=(
                    f"Target branch '{target.branch}' advanced "
                    f"{target.advanced_commits} commit(s) past validation base."
                ),
            )
        )

    return findings


def _matched(changed_paths: Sequence[str], patterns: Sequence[str]) -> list[str]:
    """Return changed paths that match any pattern via prefix / glob-prefix.

    The matcher is intentionally simple: literal equality, ``foo/`` prefix
    match (anything under ``foo/``), and ``foo/**`` glob-prefix. Matches the
    workspace owned-path overlap policy in spirit; we don't need full
    fnmatch semantics yet.
    """
    matches: list[str] = []
    for path in changed_paths:
        for pattern in patterns:
            if _path_matches(path, pattern):
                matches.append(path)
                break
    return matches


def _path_matches(path: str, pattern: str) -> bool:
    if not pattern:
        return False
    if path == pattern:
        return True
    if pattern.endswith("/**"):
        prefix = pattern[: -len("**")]
        return path.startswith(prefix) or path == prefix.rstrip("/")
    if pattern.endswith("/*"):
        prefix = pattern[: -len("*")]
        return path.startswith(prefix) and "/" not in path[len(prefix) :]
    if pattern.endswith("/"):
        return path.startswith(pattern)
    if "*" in pattern or "?" in pattern or "[" in pattern:
        wildcard_idx = min(
            i for i in (pattern.find("*"), pattern.find("?"), pattern.find("[")) if i >= 0
        )
        prefix = pattern[:wildcard_idx]
        if not prefix:
            return True
        return path.startswith(prefix)
    return path.startswith(pattern + "/")


# ── Service layer ──────────────────────────────────────────────────────────


class TargetBranchStateProvider(ABC):
    """Abstraction over "fetch the target branch state".

    Production wires in a git/gh-backed implementation; tests inject a
    stub so the refresh service is exercisable without subprocesses.
    """

    @abstractmethod
    async def fetch(
        self,
        *,
        repo_url: str,
        branch: str,
        base_sha: str,
    ) -> TargetBranchState: ...


@dataclass(frozen=True)
class StalenessRefreshResult:
    """Outcome of one ``StalenessRefreshService.refresh_candidate`` call."""

    candidate_id: str
    target: TargetBranchState
    findings: list[StalenessFinding]
    newly_added: list[StaleReason]
    newly_resolved: list[StaleReason]
    stale: bool


class StalenessRefreshService:
    """Refresh staleness state for one merge candidate."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        target_state_provider: TargetBranchStateProvider | None = None,
        policy: StalePolicy = DEFAULT_STALE_POLICY,
    ) -> None:
        self._session = session
        self._provider = target_state_provider
        self._policy = policy

    async def refresh_candidate(
        self,
        candidate_id: str,
        *,
        target: TargetBranchState | None = None,
    ) -> StalenessRefreshResult:
        candidate = await _load_candidate(self._session, candidate_id)
        if candidate is None:
            raise StalenessRefreshError(
                f"Merge candidate {candidate_id!r} not found",
            )

        target_state = target if target is not None else await self._fetch_target(candidate)
        findings = evaluate_staleness(
            candidate=_snapshot_for(candidate),
            target=target_state,
            policy=self._policy,
        )

        reasons_repo = StaleReasonRepository(self._session)
        newly_added, newly_resolved = await reasons_repo.replace_active_findings(
            workspace_id=candidate.workspace_id,
            candidate_id=candidate.id,
            attempt_id=candidate.attempt_id,
            task_id=candidate.task_id,
            findings=[
                StaleReasonCreate(
                    reason_code=f.reason_code,
                    trigger_type=f.trigger_type,
                    trigger_ref=f.trigger_ref,
                    explanation=f.explanation,
                )
                for f in findings
            ],
        )

        stale = bool(findings)
        await self._mark_candidate_stale(candidate, stale=stale)

        if newly_added:
            await self._emit_events(
                candidate=candidate,
                target=target_state,
                added=newly_added,
            )

        return StalenessRefreshResult(
            candidate_id=candidate.id,
            target=target_state,
            findings=findings,
            newly_added=list(newly_added),
            newly_resolved=list(newly_resolved),
            stale=stale,
        )

    async def _fetch_target(self, candidate: MergeCandidate) -> TargetBranchState:
        if self._provider is None:
            raise StalenessRefreshError(
                "No target branch state supplied and no provider injected",
            )
        if candidate.base_sha is None:
            raise StalenessRefreshError(
                f"Candidate {candidate.id!r} has no validation base_sha to compare",
            )
        return await self._provider.fetch(
            repo_url=candidate.repo_url,
            branch=candidate.base_branch,
            base_sha=candidate.base_sha,
        )

    async def _mark_candidate_stale(
        self,
        candidate: MergeCandidate,
        *,
        stale: bool,
    ) -> None:
        candidate.stale = stale
        candidate.stale_reason = "stale" if stale else None
        # Re-sync derived readiness flags so the merge-queue blocker reason
        # picks up the new stale state without an out-of-band refresh.
        from awf.db.repositories import _sync_candidate_readiness

        _sync_candidate_readiness(
            candidate,
            workspace=candidate.workspace,
            attempt=candidate.attempt,
            recompute_stale=False,
        )
        await self._session.flush()

    async def _emit_events(
        self,
        *,
        candidate: MergeCandidate,
        target: TargetBranchState,
        added: Iterable[StaleReason],
    ) -> None:
        repo = WorkspaceRepository(self._session)
        events = [
            WorkspaceEventCreate(
                event_type="merge_candidate.stale_detected",
                reason_code=row.reason_code,
                payload={
                    "candidate_id": candidate.id,
                    "attempt_id": candidate.attempt_id,
                    "task_id": candidate.task_id,
                    "trigger_type": row.trigger_type,
                    "trigger_ref": row.trigger_ref,
                    "explanation": row.explanation,
                    "target_branch": target.branch,
                    "target_head_sha": target.head_sha,
                    "advanced_commits": target.advanced_commits,
                    "detected_at": _isoformat(row.detected_at),
                },
            )
            for row in added
        ]
        if events:
            await repo.add_events(candidate.workspace, events=events)


class StalenessRefreshError(RuntimeError):
    """Raised when the refresh service can't proceed (missing inputs/rows)."""


def _snapshot_for(candidate: MergeCandidate) -> CandidateSnapshot:
    workspace: Workspace = candidate.workspace
    attempt: TaskAttempt = candidate.attempt
    owned = tuple(workspace.owned_paths) if workspace.owned_paths else tuple(attempt.owned_paths)
    return CandidateSnapshot(
        owned_paths=owned,
        task_class=workspace.task_class or attempt.task_class,
        base_sha=candidate.base_sha,
    )


async def _load_candidate(session: AsyncSession, candidate_id: str) -> MergeCandidate | None:
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    stmt = (
        select(MergeCandidate)
        .where(MergeCandidate.id == candidate_id)
        .options(
            selectinload(MergeCandidate.attempt),
            selectinload(MergeCandidate.workspace),
            selectinload(MergeCandidate.task),
        )
    )
    return (await session.execute(stmt)).scalar_one_or_none()


def _isoformat(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()
