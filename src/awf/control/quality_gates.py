"""Quality-gate protections for agent-authored workspace changes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from awf.db.repositories import owned_paths_overlap

PROTECTED_QUALITY_GATE_PATHS: Final[tuple[str, ...]] = (
    ".awf/workspace.yml",
    ".coveragerc",
    ".github/workflows/",
    "pyproject.toml",
    "pytest.ini",
    "setup.cfg",
    "setup.py",
    "tox.ini",
)
INTERNAL_PLAN_ARTIFACT_PREFIX: Final[str] = "docs/awf-plans/"
PLAN_ONLY_OUTPUT_REASON_CODE: Final[str] = "PLAN_ONLY_OUTPUT"


@dataclass(frozen=True)
class QualityGateViolation:
    """A protected quality-gate file changed outside task ownership."""

    path: str
    protected_pattern: str


def find_protected_quality_gate_changes(
    *,
    changed_paths: list[str] | tuple[str, ...],
    owned_paths: list[str] | tuple[str, ...],
) -> list[QualityGateViolation]:
    """Return protected quality-gate changes the task did not explicitly own.

    Agents should raise coverage by adding real tests. They should not make a
    failing workspace pass by lowering coverage, test, or CI policy. This
    helper is intentionally path-level and conservative: a task may edit these
    files only when its declared ``owned_paths`` explicitly covers them.
    """
    violations: list[QualityGateViolation] = []
    for raw_path in changed_paths:
        path = _normalize_path(raw_path)
        if not path:
            continue
        protected = _matched_protected_pattern(path)
        if protected is None:
            continue
        if _is_owned(path, owned_paths):
            continue
        violations.append(QualityGateViolation(path=path, protected_pattern=protected))
    return violations


def quality_gate_violation_message(violations: list[QualityGateViolation]) -> str:
    """Build an operator-facing failure message for protected gate edits."""
    paths = ", ".join(v.path for v in violations[:8])
    suffix = "" if len(violations) <= 8 else f", and {len(violations) - 8} more"
    return (
        "agent changed protected quality-gate file(s) outside declared owned_paths: "
        f"{paths}{suffix}. AWF will not accept lowering or bypassing coverage, "
        "test, or CI policy as a validation fix; add meaningful tests or declare "
        "explicit ownership of the policy file."
    )


def changed_paths_are_only_internal_plan_artifacts(
    changed_paths: list[str] | tuple[str, ...],
) -> bool:
    normalized = [_normalize_path(path) for path in changed_paths]
    paths = [path for path in normalized if path]
    return bool(paths) and all(_is_internal_plan_artifact_path(path) for path in paths)


def plan_only_output_message(changed_paths: list[str] | tuple[str, ...]) -> str:
    paths = [_normalize_path(path) for path in changed_paths if _normalize_path(path)]
    preview = ", ".join(paths[:8])
    suffix = "" if len(paths) <= 8 else f", and {len(paths) - 8} more"
    return (
        "agent produced only AWF plan/conformance artifact changes "
        f"({preview}{suffix}). AWF will not open a PR until the branch contains "
        "implementation, test, or user-facing documentation output for the task."
    )


def _is_internal_plan_artifact_path(path: str) -> bool:
    return path.startswith(INTERNAL_PLAN_ARTIFACT_PREFIX)


def _matched_protected_pattern(path: str) -> str | None:
    for pattern in PROTECTED_QUALITY_GATE_PATHS:
        normalized_pattern = _normalize_path(pattern)
        if normalized_pattern.endswith("/"):
            if path.startswith(normalized_pattern):
                return pattern
        elif path == normalized_pattern:
            return pattern
    return None


def _is_owned(path: str, owned_paths: list[str] | tuple[str, ...]) -> bool:
    return any(owned_paths_overlap(path, owned_path) for owned_path in owned_paths)


def _normalize_path(path: str) -> str:
    normalized = path.strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized
