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
  on target while policy marks the candidate's task class schema-sensitive.
* ``STALE_DEPENDENCY``      — a dependency file (``pyproject.toml`` etc.)
  changed while policy marks the candidate's task class dependency-sensitive.
* ``STALE_BUILD_CONFIG``    — a build-config file (``Dockerfile``,
  ``docker-compose``, CI workflows) changed while policy marks the candidate's
  task class build-config-sensitive.
* ``ADVISORY_PLAN_ARTIFACT_OVERLAP`` — workspace-specific AWF ``ws_*``
  plan/conformance artifacts under ``docs/awf-plans/`` overlapped. Visible to
  operators, but does not block merge by itself.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from awf.common.logging import get_logger
from awf.common.owned_paths import (
    internal_plan_artifact_owned_paths_from_profile,
    is_internal_plan_artifact_owned_path,
)
from awf.db.models import (
    ADVISORY_PLAN_ARTIFACT_OVERLAP_REASON,
    MergeCandidate,
    StaleReason,
    TaskAttempt,
    Workspace,
)
from awf.db.repositories import (
    StaleReasonCreate,
    StaleReasonRepository,
    WorkspaceEventCreate,
    WorkspaceRepository,
)
from awf.runtime.merge_eligibility import (
    VALIDATION_INSUFFICIENT_TIER_STALE_REASON,
    VALIDATION_MISSING_FOR_CURRENT_HEAD_STALE_REASON,
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
    internal_plan_artifact_paths: tuple[str, ...] = ()


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
    blocks_merge: bool = True
    severity: str = "blocking"


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
    schema_sensitive_task_classes: tuple[str, ...] = field(
        default_factory=lambda: ("migration_task",)
    )
    dependency_sensitive_task_classes: tuple[str, ...] = field(
        default_factory=lambda: ("dependency_task", "migration_task")
    )
    build_config_sensitive_task_classes: tuple[str, ...] = field(
        default_factory=lambda: ("build_config_task", "migration_task")
    )


REASON_TARGET_ADVANCED: Final[str] = "STALE_TARGET_ADVANCED"
REASON_OVERLAP: Final[str] = "STALE_OVERLAP"
REASON_SCHEMA: Final[str] = "STALE_SCHEMA"
REASON_DEPENDENCY: Final[str] = "STALE_DEPENDENCY"
REASON_BUILD_CONFIG: Final[str] = "STALE_BUILD_CONFIG"
REASON_PLAN_ARTIFACT_OVERLAP: Final[str] = ADVISORY_PLAN_ARTIFACT_OVERLAP_REASON
_BLOCKING_REASON_PRIORITY: Final[dict[str, int]] = {
    REASON_OVERLAP: 0,
    REASON_SCHEMA: 1,
    REASON_DEPENDENCY: 2,
    REASON_BUILD_CONFIG: 3,
    REASON_TARGET_ADVANCED: 4,
}
_DEFAULT_BLOCKING_REASON_PRIORITY: Final[int] = len(_BLOCKING_REASON_PRIORITY)
_VALIDATION_STALE_REASONS_TO_PRESERVE: Final[frozenset[str]] = frozenset(
    {
        VALIDATION_INSUFFICIENT_TIER_STALE_REASON,
        VALIDATION_MISSING_FOR_CURRENT_HEAD_STALE_REASON,
    }
)

TRIGGER_TARGET_ADVANCED: Final[str] = "target_advanced"
TRIGGER_PATH_OVERLAP: Final[str] = "path_overlap"
TRIGGER_SCHEMA_CHANGED: Final[str] = "schema_changed"
TRIGGER_DEPENDENCY_CHANGED: Final[str] = "dependency_changed"
TRIGGER_BUILD_CONFIG_CHANGED: Final[str] = "build_config_changed"
TRIGGER_PLAN_ARTIFACT_OVERLAP: Final[str] = "plan_artifact_overlap"

DEFAULT_STALE_POLICY: Final[StalePolicy] = StalePolicy(
    schema_paths=(
        "migrations/",
        "migration/",
        "schema/",
        "schemas/",
        "models/",
        "model/",
        "*/migrations/**",
        "*/migration/**",
        "*/schema/**",
        "*/schemas/**",
        "*/models/**",
        "*/model/**",
        "*/models.py",
        "*/model.py",
        "db/schema.rb",
        "prisma/schema.prisma",
        "alembic.ini",
    ),
    dependency_paths=(
        "pyproject.toml",
        "uv.lock",
        "requirements.txt",
        "requirements-dev.txt",
        "poetry.lock",
        "Pipfile",
        "Pipfile.lock",
        "package.json",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
    ),
    build_config_paths=(
        "Dockerfile",
        "Dockerfile.*",
        "*/Dockerfile",
        "*/Dockerfile.*",
        "docker/",
        "docker-compose.yml",
        "docker-compose.yaml",
        "compose.yml",
        "compose.yaml",
        ".github/workflows/",
        "Makefile",
        "Taskfile.yml",
        "taskfile.yml",
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
    plan_artifact_overlap = [
        path
        for path in overlap
        if _is_plan_artifact_path(
            path,
            internal_plan_artifact_paths=candidate.internal_plan_artifact_paths,
        )
    ]
    blocking_overlap = [
        path
        for path in overlap
        if not _is_plan_artifact_path(
            path,
            internal_plan_artifact_paths=candidate.internal_plan_artifact_paths,
        )
    ]

    if _is_sensitive_task_class(
        candidate.task_class,
        policy.schema_sensitive_task_classes,
    ):
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

    if _is_sensitive_task_class(
        candidate.task_class,
        policy.dependency_sensitive_task_classes,
    ):
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

    if _is_sensitive_task_class(
        candidate.task_class,
        policy.build_config_sensitive_task_classes,
    ):
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

    for path in plan_artifact_overlap[:1]:
        findings.append(
            StalenessFinding(
                reason_code=REASON_PLAN_ARTIFACT_OVERLAP,
                trigger_type=TRIGGER_PLAN_ARTIFACT_OVERLAP,
                trigger_ref=path,
                explanation=(
                    f"AWF plan/conformance artifact '{path}' changed on target "
                    f"branch '{target.branch}'."
                ),
                blocks_merge=False,
                severity="advisory",
            )
        )

    for path in blocking_overlap:
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
        not any(finding.blocks_merge for finding in findings)
        and target.advanced_commits > 0
        and candidate.task_class not in policy.lenient_task_classes
        and not _target_changes_are_only_plan_artifacts(
            target.changed_paths,
            internal_plan_artifact_paths=candidate.internal_plan_artifact_paths,
        )
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


def _is_sensitive_task_class(
    task_class: str | None,
    sensitive_task_classes: Sequence[str],
) -> bool:
    return task_class is not None and task_class in sensitive_task_classes


def _matched(changed_paths: Sequence[str], patterns: Sequence[str]) -> list[str]:
    """Return changed paths that match any pattern via prefix / glob-prefix.

    The matcher is intentionally simple: literal equality, ``foo/`` prefix
    match (anything under ``foo/``), ``foo/**`` glob-prefix, and bounded
    ``fnmatch`` patterns for policy defaults such as ``*/models/**``. Matches
    the workspace owned-path overlap policy in spirit without requiring callers
    to normalize paths first.
    """
    matches: list[str] = []
    for path in changed_paths:
        for pattern in patterns:
            if _path_matches(path, pattern):
                matches.append(path)
                break
    return matches


def _path_matches(path: str, pattern: str) -> bool:
    """Return whether ``path`` matches ``pattern``.

    Prefix branches only handle patterns with a trailing wildcard; mixed
    wildcard patterns fall through to the bounded ``fnmatchcase`` fallback.
    """
    if not pattern:
        return False
    if path == pattern:
        return True
    if pattern.endswith("/**") and not _has_wildcard(pattern[: -len("/**")]):
        prefix = pattern[: -len("**")]
        return path.startswith(prefix) or path == prefix.rstrip("/")
    if pattern.endswith("/*") and not _has_wildcard(pattern[: -len("/*")]):
        prefix = pattern[: -len("*")]
        return path.startswith(prefix) and "/" not in path[len(prefix) :]
    if pattern.endswith("/"):
        return path.startswith(pattern)
    if _has_wildcard(pattern):
        return fnmatchcase(path, pattern)
    return path.startswith(pattern + "/")


def _is_plan_artifact_path(
    path: str,
    *,
    internal_plan_artifact_paths: Iterable[str] = (),
) -> bool:
    """Return true when a changed path is an AWF internal plan artifact."""
    return is_internal_plan_artifact_owned_path(
        path,
        internal_plan_artifact_paths=internal_plan_artifact_paths,
    )


def _target_changes_are_only_plan_artifacts(
    changed_paths: Sequence[str],
    *,
    internal_plan_artifact_paths: Iterable[str] = (),
) -> bool:
    """Return true when all target changes are advisory planning artifacts."""
    return bool(changed_paths) and all(
        _is_plan_artifact_path(
            path,
            internal_plan_artifact_paths=internal_plan_artifact_paths,
        )
        for path in changed_paths
    )


def _has_wildcard(pattern: str) -> bool:
    return "*" in pattern or "?" in pattern or "[" in pattern


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
    ) -> TargetBranchState:
        """Fetch target-branch changes relative to a candidate base SHA."""
        ...


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
        """Initialize the refresh service with persistence and policy hooks."""
        self._session = session
        self._provider = target_state_provider
        self._policy = policy

    async def refresh_candidate(
        self,
        candidate_id: str,
        *,
        target: TargetBranchState | None = None,
    ) -> StalenessRefreshResult:
        """Refresh persisted stale reasons for one merge candidate."""
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

        blocking_reason = _primary_blocking_reason(findings)
        stale = blocking_reason is not None
        await self._mark_candidate_stale(
            candidate,
            stale=stale,
            stale_reason=blocking_reason,
        )

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
        stale_reason: str | None,
    ) -> None:
        from awf.db.repositories import sync_candidate_readiness
        from awf.runtime.merge_eligibility import compute_stale_reason

        validation_reason, _ = compute_stale_reason(candidate.workspace)
        existing_validation_reason = (
            candidate.stale_reason
            if candidate.stale_reason in _VALIDATION_STALE_REASONS_TO_PRESERVE
            else None
        )
        next_stale = stale or validation_reason is not None
        next_stale_reason = (
            validation_reason
            or (stale_reason if stale else None)
            or (existing_validation_reason if stale else None)
            or ("stale" if stale else None)
        )
        if candidate.stale != next_stale or candidate.stale_reason != next_stale_reason:
            candidate.stale = next_stale
            candidate.stale_reason = next_stale_reason
        # Re-sync derived readiness flags so the merge-queue blocker reason
        # picks up the new stale state without an out-of-band refresh.
        sync_candidate_readiness(
            candidate,
            workspace=candidate.workspace,
            attempt=candidate.attempt,
            sync_validation_staleness=False,
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
                    "severity": row.severity,
                    "blocks_merge": row.blocks_merge,
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
    profile = workspace.resolved_profile
    concrete_plan_artifacts = internal_plan_artifact_owned_paths_from_profile(
        profile,
        workspace_id=workspace.id,
    )
    wildcard_plan_artifacts = internal_plan_artifact_owned_paths_from_profile(profile)
    return CandidateSnapshot(
        owned_paths=owned,
        task_class=workspace.task_class or attempt.task_class,
        base_sha=candidate.base_sha,
        # Target changes may contain sibling workspace artifacts rendered from the
        # same profile template, so staleness classification needs the wildcard
        # shape in addition to this workspace's concrete artifact path.
        internal_plan_artifact_paths=tuple(
            dict.fromkeys(concrete_plan_artifacts + wildcard_plan_artifacts)
        ),
    )


def _primary_blocking_reason(findings: Sequence[StalenessFinding]) -> str | None:
    primary = min(
        ((index, finding) for index, finding in enumerate(findings) if finding.blocks_merge),
        key=lambda item: (
            _BLOCKING_REASON_PRIORITY.get(
                item[1].reason_code,
                _DEFAULT_BLOCKING_REASON_PRIORITY,
            ),
            item[0],
        ),
        default=None,
    )
    return primary[1].reason_code if primary is not None else None


async def _load_candidate(session: AsyncSession, candidate_id: str) -> MergeCandidate | None:
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    stmt = (
        select(MergeCandidate)
        .where(MergeCandidate.id == candidate_id)
        .options(
            selectinload(MergeCandidate.attempt),
            selectinload(MergeCandidate.workspace).options(
                selectinload(Workspace.operations),
                selectinload(Workspace.validation_runs),
            ),
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
