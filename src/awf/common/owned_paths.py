"""Shared owned-path classification helpers."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Final

INTERNAL_PLAN_ARTIFACT_DIR: Final = "docs/awf-plans"
INTERNAL_PLAN_ARTIFACT_NAME_RE: Final = re.compile(
    r"^ws_[A-Za-z0-9_*?-]+(?:\.md|(?:\.conformance)?\.json)$"
)


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
    prefix = f"{INTERNAL_PLAN_ARTIFACT_DIR}/"
    if not normalized.startswith(prefix):
        return False
    filename = normalized.removeprefix(prefix)
    return "/" not in filename and INTERNAL_PLAN_ARTIFACT_NAME_RE.fullmatch(filename) is not None


def interworkspace_owned_paths(paths: Iterable[str]) -> tuple[str, ...]:
    """Owned paths that should participate in inter-workspace dependency checks."""
    return tuple(
        path
        for path in paths
        if normalize_owned_path(path) != "" and not is_internal_plan_artifact_owned_path(path)
    )
