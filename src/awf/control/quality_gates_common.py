"""Shared helpers for protected quality-gate diff classifiers."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast


@dataclass(frozen=True)
class ProtectedFileDiff:
    """Local old/new content for classifying a protected file change."""

    path: str
    old_text: str | None
    new_text: str | None


@dataclass(frozen=True)
class QualityGateViolation:
    """A protected quality-gate file changed outside task ownership."""

    path: str
    protected_pattern: str
    section: str | None = None
    line: int | None = None
    reason: str = "protected quality-gate file changed outside declared owned_paths"


def _normalize_path(path: str) -> str:
    normalized = path.strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _violation(
    *,
    path: str,
    protected_pattern: str,
    section: str,
    line: int | None,
    reason: str,
) -> QualityGateViolation:
    return QualityGateViolation(
        path=path,
        protected_pattern=protected_pattern,
        section=section,
        line=line,
        reason=reason,
    )


def _absent_protected_file_content_reason(
    *,
    old_text: str | None,
    new_text: str | None,
    added_reason: str,
    deleted_reason: str,
    unavailable_reason: str,
) -> str:
    if old_text is None and new_text is not None:
        return added_reason
    if old_text is not None and new_text is None:
        return deleted_reason
    return unavailable_reason


def _nested_value(data: Mapping[str, Any], keys: tuple[str, ...]) -> object:
    current: object = data
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _format_number(value: float) -> str:
    return str(int(value)) if value.is_integer() else f"{value:g}"


def _format_toml_policy_value(value: object) -> str:
    if value is None:
        return "unset"
    if _is_number(value):
        return _format_number(float(cast(int | float, value)))
    if isinstance(value, str):
        return repr(value)
    return str(value)


def _line_containing(text: str, needle: str) -> int | None:
    for index, line in enumerate(text.splitlines(), start=1):
        if needle in line:
            return index
    return None


def _line_matching(text: str, pattern: str) -> int | None:
    compiled = re.compile(pattern)
    for index, line in enumerate(text.splitlines(), start=1):
        if compiled.search(line):
            return index
    return None
