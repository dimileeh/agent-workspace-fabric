"""Out-of-scope change policy evaluation.

This module compares the files changed by a workspace PR against the task's
declared ``owned_paths`` plus an explicit allowlist. Owned-path overlap between
workspaces remains advisory; this policy only concerns files the current task
actually changed outside its declared scope.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from awf.db.models import MergeCandidate, PolicyFinding, TaskAttempt, Workspace
from awf.db.repositories import (
    PolicyFindingCreate,
    PolicyFindingRepository,
    WorkspaceEventCreate,
    WorkspaceRepository,
    owned_paths_overlap,
    sync_candidate_readiness,
)
from awf.profiles.models import OutOfScopeChangePolicy

OUT_OF_SCOPE_CHANGE_CODE: Final[str] = "OUT_OF_SCOPE_CHANGE"
POLICY_FINDING_EVENT_TYPE: Final[str] = "workspace.policy_finding"
PolicySeverity = Literal["warning", "blocking"]


@dataclass(frozen=True)
class OutOfScopeChangeFinding:
    reason_code: str
    path: str
    severity: PolicySeverity
    explanation: str
    details: dict[str, object]


@dataclass(frozen=True)
class ScopePolicyRefreshResult:
    candidate_id: str
    changed_paths: tuple[str, ...]
    findings: list[OutOfScopeChangeFinding]
    newly_added: list[PolicyFinding]
    newly_resolved: list[PolicyFinding]
    policy_blocked: bool


def evaluate_out_of_scope_changes(
    *,
    changed_paths: Sequence[str],
    owned_paths: Sequence[str],
    policy: OutOfScopeChangePolicy,
) -> list[OutOfScopeChangeFinding]:
    """Return one finding per changed file outside owned paths and allowlist."""
    severity: PolicySeverity = "blocking" if policy.mode == "block" else "warning"
    normalized_changed_paths = _normalized_unique_paths(changed_paths)
    findings: list[OutOfScopeChangeFinding] = []
    for path in normalized_changed_paths:
        if _matches_any(path, owned_paths):
            continue
        if _matches_any(path, policy.allowlist_patterns):
            continue
        findings.append(
            OutOfScopeChangeFinding(
                reason_code=OUT_OF_SCOPE_CHANGE_CODE,
                path=path,
                severity=severity,
                explanation=(
                    f"Changed file '{path}' is outside declared owned_paths "
                    "and out-of-scope allowlist."
                ),
                details={
                    "owned_paths": list(owned_paths),
                    "allowlist_patterns": list(policy.allowlist_patterns),
                    "mode": policy.mode.value,
                },
            )
        )
    return findings


def out_of_scope_policy_for_workspace(workspace: Workspace) -> OutOfScopeChangePolicy:
    """Resolve the effective out-of-scope policy for a workspace snapshot."""
    profile_policy = _policy_from_profile_section(
        _nested_dict(workspace.resolved_profile, "quality", "out_of_scope_changes")
    )
    task_policy = _policy_from_profile_section(
        _nested_dict(getattr(workspace, "task_policy", None), "out_of_scope_changes")
        or _nested_dict(workspace.resolved_profile, "task_policy", "out_of_scope_changes")
    )

    mode = "block" if profile_policy.mode == "block" or task_policy.mode == "block" else "warn"
    allowlist_patterns = [
        *profile_policy.allowlist_patterns,
        *task_policy.allowlist_patterns,
    ]
    return OutOfScopeChangePolicy(
        mode=mode,
        allowlist_patterns=list(dict.fromkeys(allowlist_patterns)),
    )


class ScopePolicyRefreshService:
    """Refresh active out-of-scope findings for one merge candidate."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def refresh_workspace_open_candidate(
        self,
        workspace_id: str,
        *,
        changed_paths: Sequence[str],
    ) -> ScopePolicyRefreshResult | None:
        stmt = (
            select(MergeCandidate.id)
            .where(
                MergeCandidate.workspace_id == workspace_id,
                MergeCandidate.status == "open",
            )
            .order_by(MergeCandidate.updated_at.desc(), MergeCandidate.id.desc())
            .limit(1)
        )
        candidate_id = (await self._session.execute(stmt)).scalar_one_or_none()
        if candidate_id is None:
            return None
        return await self.refresh_candidate(candidate_id, changed_paths=changed_paths)

    async def refresh_candidate(
        self,
        candidate_id: str,
        *,
        changed_paths: Sequence[str],
    ) -> ScopePolicyRefreshResult:
        candidate = await _load_candidate(self._session, candidate_id)
        if candidate is None:
            raise ScopePolicyRefreshError(f"Merge candidate {candidate_id!r} not found")

        normalized_changed_paths = tuple(_normalized_unique_paths(changed_paths))
        policy = out_of_scope_policy_for_workspace(candidate.workspace)
        owned_paths = _owned_paths_for(candidate)
        findings = evaluate_out_of_scope_changes(
            changed_paths=normalized_changed_paths,
            owned_paths=owned_paths,
            policy=policy,
        )
        repo = PolicyFindingRepository(self._session)
        newly_added, newly_resolved = await repo.replace_active_findings(
            workspace_id=candidate.workspace_id,
            candidate_id=candidate.id,
            attempt_id=candidate.attempt_id,
            task_id=candidate.task_id,
            reason_code=OUT_OF_SCOPE_CHANGE_CODE,
            findings=[
                PolicyFindingCreate(
                    reason_code=f.reason_code,
                    severity=f.severity,
                    subject_path=f.path,
                    explanation=f.explanation,
                    details=f.details,
                )
                for f in findings
            ],
        )

        active_findings = await repo.list_active_for_candidate(candidate.id)
        policy_blocked = any(f.severity == "blocking" for f in active_findings)
        if candidate.policy_blocked != policy_blocked:
            candidate.policy_blocked = policy_blocked
        sync_candidate_readiness(
            candidate,
            workspace=candidate.workspace,
            attempt=candidate.attempt,
        )
        if newly_added:
            await self._emit_events(candidate=candidate, added=newly_added)
        await self._session.flush()
        return ScopePolicyRefreshResult(
            candidate_id=candidate.id,
            changed_paths=normalized_changed_paths,
            findings=findings,
            newly_added=list(newly_added),
            newly_resolved=list(newly_resolved),
            policy_blocked=policy_blocked,
        )

    async def _emit_events(
        self,
        *,
        candidate: MergeCandidate,
        added: Iterable[PolicyFinding],
    ) -> None:
        repo = WorkspaceRepository(self._session)
        events = [
            WorkspaceEventCreate(
                event_type=POLICY_FINDING_EVENT_TYPE,
                reason_code=row.reason_code,
                payload={
                    "finding_id": row.id,
                    "candidate_id": candidate.id,
                    "attempt_id": candidate.attempt_id,
                    "task_id": candidate.task_id,
                    "severity": row.severity,
                    "path": row.subject_path,
                    "explanation": row.explanation,
                    "details": row.details,
                    "detected_at": _isoformat(row.detected_at),
                },
            )
            for row in added
        ]
        if events:
            await repo.add_events(candidate.workspace, events=events)


class ScopePolicyRefreshError(RuntimeError):
    """Raised when policy refresh cannot proceed."""


def _policy_from_profile_section(value: dict[str, object] | None) -> OutOfScopeChangePolicy:
    if value is None:
        return OutOfScopeChangePolicy()
    mode = value.get("mode")
    allowlist = value.get("allowlist_patterns")
    return OutOfScopeChangePolicy(
        mode="block" if mode == "block" else "warn",
        allowlist_patterns=[item for item in allowlist if isinstance(item, str)]
        if isinstance(allowlist, list)
        else [],
    )


def _nested_dict(
    value: dict[str, object] | None,
    first: str,
    second: str | None = None,
) -> dict[str, object] | None:
    if value is None:
        return None
    first_value = value.get(first)
    if second is None:
        return first_value if isinstance(first_value, dict) else None
    if not isinstance(first_value, dict):
        return None
    second_value = first_value.get(second)
    return second_value if isinstance(second_value, dict) else None


def _owned_paths_for(candidate: MergeCandidate) -> tuple[str, ...]:
    workspace: Workspace = candidate.workspace
    attempt: TaskAttempt = candidate.attempt
    return tuple(workspace.owned_paths) if workspace.owned_paths else tuple(attempt.owned_paths)


async def _load_candidate(session: AsyncSession, candidate_id: str) -> MergeCandidate | None:
    stmt = (
        select(MergeCandidate)
        .where(MergeCandidate.id == candidate_id)
        .options(
            selectinload(MergeCandidate.attempt),
            selectinload(MergeCandidate.workspace).selectinload(Workspace.operations),
            selectinload(MergeCandidate.workspace).selectinload(Workspace.validation_runs),
            selectinload(MergeCandidate.workspace).selectinload(Workspace.policy_findings),
            selectinload(MergeCandidate.task),
        )
    )
    return (await session.execute(stmt)).scalar_one_or_none()


def _normalized_unique_paths(paths: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(path for raw in paths if (path := _normalize_path(raw))))


def _normalize_path(path: str) -> str:
    segments: list[str] = []
    for segment in path.strip().replace("\\", "/").split("/"):
        if segment in {"", "."}:
            continue
        if segment == "..":
            if segments:
                segments.pop()
            continue
        segments.append(segment)
    return "/".join(segments)


def _matches_any(path: str, patterns: Sequence[str]) -> bool:
    return any(owned_paths_overlap(pattern, path) for pattern in patterns)


def _isoformat(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()
