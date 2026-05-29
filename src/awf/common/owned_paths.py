"""Shared owned-path classification helpers."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

INTERNAL_PLAN_ARTIFACT_PREFIX: Final = "docs/awf-plans"


def normalize_owned_path(path: str) -> str:
    """Normalize owned-path strings without interpreting glob syntax."""
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


def is_internal_plan_artifact_owned_path(path: str) -> bool:
    """Return true for AWF-generated planning/conformance artifact paths."""
    normalized = normalize_owned_path(path)
    return normalized == INTERNAL_PLAN_ARTIFACT_PREFIX or normalized.startswith(
        f"{INTERNAL_PLAN_ARTIFACT_PREFIX}/"
    )


def interworkspace_owned_paths(paths: Iterable[str]) -> tuple[str, ...]:
    """Owned paths that should participate in inter-workspace dependency checks."""
    return tuple(
        path
        for path in paths
        if normalize_owned_path(path) != "" and not is_internal_plan_artifact_owned_path(path)
    )
