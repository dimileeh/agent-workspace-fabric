"""Small helpers for supply-chain policy evaluation."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Final, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from awf.db.models import MergeCandidate, Workspace
from awf.db.repositories import owned_paths_overlap

PolicySeverity = Literal["warning", "blocking"]

_LOCKFILE_NAMES: Final[frozenset[str]] = frozenset(
    {
        "package-lock.json",
        "npm-shrinkwrap.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "bun.lock",
        "bun.lockb",
        "poetry.lock",
        "uv.lock",
        "Pipfile.lock",
        "Cargo.lock",
        "go.sum",
        "composer.lock",
        "Gemfile.lock",
        "mix.lock",
        "gradle.lockfile",
        "packages.lock.json",
    }
)
_URL_CREDENTIAL_PATTERN: Final[re.Pattern[str]] = re.compile(r"(https?://)[^/@\s]+(?::[^/@\s]+)?@")


def _command_token_segments(
    tokens: Sequence[str],
    separators: frozenset[str],
) -> list[list[str]]:
    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in separators:
            if current:
                segments.append(current)
                current = []
            continue
        current.append(token)
    if current:
        segments.append(current)
    return segments


def _severity(mode: str) -> PolicySeverity:
    return "blocking" if mode == "block" else "warning"


def _redact_command_excerpt(command: str) -> str:
    return _URL_CREDENTIAL_PATTERN.sub(r"\1[redacted]@", command).strip()[:300]


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


def _is_lockfile_path(path: str) -> bool:
    name = PurePosixPath(path).name
    return name in _LOCKFILE_NAMES or name.endswith(".lock")


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


def _isoformat(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


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


async def _load_open_candidate_for_workspace(
    session: AsyncSession,
    workspace_id: str,
) -> MergeCandidate | None:
    stmt = (
        select(MergeCandidate)
        .where(
            MergeCandidate.workspace_id == workspace_id,
            MergeCandidate.status == "open",
        )
        .order_by(MergeCandidate.updated_at.desc(), MergeCandidate.id.desc())
        .options(
            selectinload(MergeCandidate.attempt),
            selectinload(MergeCandidate.workspace).selectinload(Workspace.operations),
            selectinload(MergeCandidate.workspace).selectinload(Workspace.validation_runs),
            selectinload(MergeCandidate.workspace).selectinload(Workspace.policy_findings),
            selectinload(MergeCandidate.task),
        )
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()
