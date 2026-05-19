"""Quality-gate protections for agent-authored workspace changes."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, cast

import yaml

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
_WORKFLOW_PREFIX: Final[str] = ".github/workflows/"
_COMMENT_STEP_MARKERS: Final[tuple[str, ...]] = (
    "comment",
    "pr comment",
    "pr-comment",
    "notify",
    "notification",
)
_INFORMATIONAL_MARKERS: Final[tuple[str, ...]] = (
    "comment",
    "pr comment",
    "pr-comment",
    "notify",
    "notification",
    "info",
    "informational",
    "summary",
    "report",
)
_VALIDATION_COMMAND_TOKEN_RE: Final = re.compile(
    r"(?<![A-Za-z0-9_-])"
    r"(?:pytest|ruff|mypy|coverage|cov|lint|build|deploy|publish|release)"
    r"(?![A-Za-z0-9_-])"
)
_VALIDATION_TEST_COMMAND_RE: Final = re.compile(
    r"(?<![A-Za-z0-9_./-])"
    r"(?:npm|pnpm|yarn|bun|go|cargo|make|mvn|gradle|gradlew|tox|nox|uv|poetry|pipenv)"
    r"(?:\s+(?:run|exec|--?[A-Za-z0-9_.=:/-]+))*"
    r"\s+test(?:\s|$)"
)
_VALIDATION_UNITTEST_COMMAND_RE: Final = re.compile(
    r"(?<![A-Za-z0-9_./-])python(?:3(?:\.\d+)?)?\s+-m\s+unittest(?:\s|$)"
)
_PINNED_WORKFLOW_USES_SHA_RE: Final = re.compile(r"^[0-9a-fA-F]{40}$")
_PINNED_WORKFLOW_USES_VERSION_RE: Final = re.compile(
    r"^[vV]?(?:\d+|\d+\.\d+|\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)$"
)
_PYPROJECT_POLICY_SECTIONS: Final[tuple[tuple[str, ...], ...]] = (
    ("build-system",),
    ("tool", "hatch"),
    ("tool", "pytest"),
    ("tool", "coverage"),
    ("tool", "ruff"),
    ("tool", "mypy"),
)
_ALLOWED_PROJECT_METADATA_KEYS: Final[frozenset[str]] = frozenset(
    {
        "name",
        "version",
        "description",
        "readme",
        "requires-python",
        "license",
        "authors",
        "maintainers",
        "keywords",
        "classifiers",
        "urls",
        "dependencies",
        "optional-dependencies",
    }
)


@dataclass(frozen=True)
class ProtectedFileDiff:
    """Local old/new content for classifying a protected file change."""

    path: str
    old_text: str | None
    new_text: str | None
    unified_diff: str | None = None


@dataclass(frozen=True)
class QualityGateViolation:
    """A protected quality-gate file changed outside task ownership."""

    path: str
    protected_pattern: str
    section: str | None = None
    line: int | None = None
    reason: str = "protected quality-gate file changed outside declared owned_paths"


def find_protected_quality_gate_changes(
    *,
    changed_paths: list[str] | tuple[str, ...],
    owned_paths: list[str] | tuple[str, ...],
    protected_file_diffs: Mapping[str, ProtectedFileDiff] | None = None,
) -> list[QualityGateViolation]:
    """Return protected quality-gate changes the task did not explicitly own.

    Agents should raise coverage by adding real tests. They should not make a
    failing workspace pass by lowering coverage, test, or CI policy. When local
    old/new file content is available, pyproject and workflow changes are
    classified semantically so explicitly safe edits can proceed. Missing or
    unparseable classifier input still fails closed.
    """
    diff_by_path = {
        _normalize_path(path): diff for path, diff in (protected_file_diffs or {}).items()
    }
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
        if path == "pyproject.toml":
            diff = diff_by_path.get(path)
            if diff is None:
                violations.append(
                    _violation(
                        path=path,
                        protected_pattern=protected,
                        section=path,
                        line=None,
                        reason="diff unavailable for protected pyproject.toml change",
                    )
                )
                continue
            violations.extend(_classify_pyproject_change(diff, protected))
            continue
        if _is_workflow_yaml_path(path):
            diff = diff_by_path.get(path)
            if diff is None:
                violations.append(
                    _violation(
                        path=path,
                        protected_pattern=protected,
                        section=path,
                        line=None,
                        reason="diff unavailable for protected workflow change",
                    )
                )
                continue
            violations.extend(_classify_workflow_change(diff, protected))
            continue
        violations.append(
            _violation(
                path=path,
                protected_pattern=protected,
                section=path,
                line=None,
                reason="protected quality-gate file changed outside declared owned_paths",
            )
        )
    return violations


def quality_gate_violation_message(violations: list[QualityGateViolation]) -> str:
    """Build an operator-facing failure message for protected gate edits."""
    details = "\n".join(f"- {_format_violation_detail(v)}" for v in violations[:8])
    suffix = "" if len(violations) <= 8 else f"\n- ... and {len(violations) - 8} more"
    return (
        "agent changed protected quality-gate file(s) outside declared owned_paths:\n"
        f"{details}{suffix}\n"
        "AWF only allows narrowly classified safe protected-file edits without "
        "explicit ownership. Add meaningful tests or declare explicit ownership "
        "of the policy file when an operator-approved policy change is required."
    )


def quality_gate_violation_details(
    violations: Sequence[QualityGateViolation],
) -> list[dict[str, object]]:
    """Return stable event payload details for quality-gate violations."""
    return [
        {
            "path": violation.path,
            "protected_pattern": violation.protected_pattern,
            "section": violation.section,
            "line": violation.line,
            "reason": violation.reason,
        }
        for violation in violations
    ]


def protected_quality_gate_pattern(path: str) -> str | None:
    return _matched_protected_pattern(_normalize_path(path))


def requires_protected_file_diff(path: str) -> bool:
    normalized = _normalize_path(path)
    return normalized == "pyproject.toml" or _is_workflow_yaml_path(normalized)


def diff_classified_protected_paths(changed_paths: Sequence[str]) -> tuple[str, ...]:
    paths: list[str] = []
    for raw_path in changed_paths:
        path = _normalize_path(raw_path)
        if path and requires_protected_file_diff(path):
            paths.append(path)
    return tuple(dict.fromkeys(paths))


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


def _format_violation_detail(violation: QualityGateViolation) -> str:
    section = violation.section or violation.protected_pattern
    line = f"line {violation.line}" if violation.line is not None else "line unknown"
    return f"{violation.path} :: {section} :: {line} :: {violation.reason}"


def _classify_pyproject_change(
    diff: ProtectedFileDiff,
    protected_pattern: str,
) -> list[QualityGateViolation]:
    path = _normalize_path(diff.path)
    if diff.old_text is None or diff.new_text is None:
        return [
            _violation(
                path=path,
                protected_pattern=protected_pattern,
                section="pyproject.toml",
                line=None,
                reason="could not read old and new pyproject.toml content for classification",
            )
        ]
    if diff.old_text == diff.new_text:
        return []

    old_doc, old_error = _parse_toml(diff.old_text, path, protected_pattern)
    if old_error is not None:
        return [old_error]
    new_doc, new_error = _parse_toml(diff.new_text, path, protected_pattern)
    if new_error is not None:
        return [new_error]

    violations: list[QualityGateViolation] = []
    assert old_doc is not None
    assert new_doc is not None
    violations.extend(
        _pyproject_policy_section_violations(
            path=path,
            protected_pattern=protected_pattern,
            old_doc=old_doc,
            new_doc=new_doc,
            old_text=diff.old_text,
            new_text=diff.new_text,
        )
    )
    violations.extend(
        _pyproject_dependency_violations(
            path=path,
            protected_pattern=protected_pattern,
            old_doc=old_doc,
            new_doc=new_doc,
            old_text=diff.old_text,
            new_text=diff.new_text,
        )
    )
    violations.extend(
        _pyproject_unknown_change_violations(
            path=path,
            protected_pattern=protected_pattern,
            old_doc=old_doc,
            new_doc=new_doc,
            old_text=diff.old_text,
            new_text=diff.new_text,
        )
    )
    return violations


def _parse_toml(
    text: str,
    path: str,
    protected_pattern: str,
) -> tuple[Mapping[str, Any] | None, QualityGateViolation | None]:
    try:
        return tomllib.loads(text), None
    except tomllib.TOMLDecodeError as exc:
        line = getattr(exc, "lineno", None)
        return None, _violation(
            path=path,
            protected_pattern=protected_pattern,
            section="pyproject.toml",
            line=line if isinstance(line, int) else None,
            reason=f"could not parse pyproject.toml safely: {exc}",
        )


def _pyproject_policy_section_violations(
    *,
    path: str,
    protected_pattern: str,
    old_doc: Mapping[str, Any],
    new_doc: Mapping[str, Any],
    old_text: str,
    new_text: str,
) -> list[QualityGateViolation]:
    violations: list[QualityGateViolation] = []
    for section_keys in _PYPROJECT_POLICY_SECTIONS:
        old_value = _nested_value(old_doc, section_keys)
        new_value = _nested_value(new_doc, section_keys)
        if old_value == new_value:
            continue
        section = ".".join(section_keys)
        if section_keys == ("tool", "coverage"):
            old_fail_under = _nested_value(old_doc, ("tool", "coverage", "report", "fail_under"))
            new_fail_under = _nested_value(new_doc, ("tool", "coverage", "report", "fail_under"))
            if _is_number(old_fail_under) and _is_number(new_fail_under):
                old_number = float(cast(int | float, old_fail_under))
                new_number = float(cast(int | float, new_fail_under))
                if new_number < old_number:
                    violations.append(
                        _violation(
                            path=path,
                            protected_pattern=protected_pattern,
                            section="tool.coverage.report.fail_under",
                            line=_line_for_toml_key(
                                new_text,
                                section="tool.coverage.report",
                                key="fail_under",
                            ),
                            reason=(
                                "coverage fail_under lowered from "
                                f"{_format_number(old_number)} to {_format_number(new_number)}"
                            ),
                        )
                    )
                    continue
        line_text = new_text if new_value is not None else old_text
        violations.append(
            _violation(
                path=path,
                protected_pattern=protected_pattern,
                section=section,
                line=_line_for_toml_section(line_text, section),
                reason=f"protected pyproject policy section changed: {section}",
            )
        )
    return violations


def _pyproject_dependency_violations(
    *,
    path: str,
    protected_pattern: str,
    old_doc: Mapping[str, Any],
    new_doc: Mapping[str, Any],
    old_text: str,
    new_text: str,
) -> list[QualityGateViolation]:
    violations: list[QualityGateViolation] = []
    violations.extend(
        _dependency_list_violations(
            path=path,
            protected_pattern=protected_pattern,
            section="project.dependencies",
            old_value=_nested_value(old_doc, ("project", "dependencies")),
            new_value=_nested_value(new_doc, ("project", "dependencies")),
            old_text=old_text,
            new_text=new_text,
        )
    )
    violations.extend(
        _dependency_group_violations(
            path=path,
            protected_pattern=protected_pattern,
            section_prefix="project.optional-dependencies",
            old_value=_nested_value(old_doc, ("project", "optional-dependencies")),
            new_value=_nested_value(new_doc, ("project", "optional-dependencies")),
            old_text=old_text,
            new_text=new_text,
        )
    )
    violations.extend(
        _dependency_group_violations(
            path=path,
            protected_pattern=protected_pattern,
            section_prefix="dependency-groups",
            old_value=_nested_value(old_doc, ("dependency-groups",)),
            new_value=_nested_value(new_doc, ("dependency-groups",)),
            old_text=old_text,
            new_text=new_text,
        )
    )
    return violations


def _dependency_group_violations(
    *,
    path: str,
    protected_pattern: str,
    section_prefix: str,
    old_value: object,
    new_value: object,
    old_text: str,
    new_text: str,
) -> list[QualityGateViolation]:
    if old_value is None and new_value is None:
        return []
    if old_value is not None and not isinstance(old_value, Mapping):
        return [
            _violation(
                path=path,
                protected_pattern=protected_pattern,
                section=section_prefix,
                line=_line_for_toml_section(old_text, section_prefix),
                reason=f"dependency group section has unsupported format: {section_prefix}",
            )
        ]
    if new_value is not None and not isinstance(new_value, Mapping):
        return [
            _violation(
                path=path,
                protected_pattern=protected_pattern,
                section=section_prefix,
                line=_line_for_toml_section(new_text, section_prefix),
                reason=f"dependency group section has unsupported format: {section_prefix}",
            )
        ]
    old_groups = cast(Mapping[str, object], old_value or {})
    new_groups = cast(Mapping[str, object], new_value or {})
    violations: list[QualityGateViolation] = []
    for group in sorted(old_groups):
        section = f"{section_prefix}.{group}"
        if group not in new_groups:
            violations.append(
                _violation(
                    path=path,
                    protected_pattern=protected_pattern,
                    section=section,
                    line=_line_for_toml_section(old_text, section),
                    reason=f"dependency group removed: {section}",
                )
            )
            continue
        violations.extend(
            _dependency_list_violations(
                path=path,
                protected_pattern=protected_pattern,
                section=section,
                old_value=old_groups[group],
                new_value=new_groups[group],
                old_text=old_text,
                new_text=new_text,
            )
        )
    for group in sorted(set(new_groups) - set(old_groups)):
        section = f"{section_prefix}.{group}"
        new_dependencies = _dependency_map(new_groups[group])
        if new_dependencies is None:
            violations.append(
                _violation(
                    path=path,
                    protected_pattern=protected_pattern,
                    section=section,
                    line=_line_for_toml_section(new_text, section),
                    reason=f"dependency group has unsupported format: {section}",
                )
            )
    return violations


def _dependency_list_violations(
    *,
    path: str,
    protected_pattern: str,
    section: str,
    old_value: object,
    new_value: object,
    old_text: str,
    new_text: str,
) -> list[QualityGateViolation]:
    old_dependencies = _dependency_map(old_value)
    new_dependencies = _dependency_map(new_value)
    if old_dependencies is None or new_dependencies is None:
        line_text = new_text if new_dependencies is None else old_text
        return [
            _violation(
                path=path,
                protected_pattern=protected_pattern,
                section=section,
                line=_line_for_toml_section(line_text, section),
                reason=f"dependency section has unsupported format: {section}",
            )
        ]
    violations: list[QualityGateViolation] = []
    for name, old_raw in old_dependencies.items():
        if name not in new_dependencies:
            violations.append(
                _violation(
                    path=path,
                    protected_pattern=protected_pattern,
                    section=section,
                    line=_line_containing(old_text, old_raw),
                    reason=f"dependency removed: {name}",
                )
            )
            continue
        if old_raw != new_dependencies[name]:
            violations.append(
                _violation(
                    path=path,
                    protected_pattern=protected_pattern,
                    section=section,
                    line=_line_containing(new_text, new_dependencies[name])
                    or _line_containing(old_text, old_raw),
                    reason=f"dependency changed: {name}",
                )
            )
    return violations


def _dependency_map(value: object) -> dict[str, str] | None:
    if value is None:
        return {}
    if not isinstance(value, list):
        return None
    dependencies: dict[str, str] = {}
    for item in value:
        if not isinstance(item, str):
            return None
        name = _dependency_name(item)
        if name is None:
            return None
        dependencies[name] = item
    return dependencies


def _dependency_name(requirement: str) -> str | None:
    match = re.match(r"\s*([A-Za-z0-9_.-]+)", requirement)
    if match is None:
        return None
    return match.group(1).replace("_", "-").lower()


def _pyproject_unknown_change_violations(
    *,
    path: str,
    protected_pattern: str,
    old_doc: Mapping[str, Any],
    new_doc: Mapping[str, Any],
    old_text: str,
    new_text: str,
) -> list[QualityGateViolation]:
    for top_key in sorted(set(old_doc) | set(new_doc)):
        old_value = old_doc.get(top_key)
        new_value = new_doc.get(top_key)
        if top_key == "project":
            violation = _project_unknown_change_violation(
                path=path,
                protected_pattern=protected_pattern,
                old_value=old_value,
                new_value=new_value,
                old_text=old_text,
                new_text=new_text,
            )
            if violation is not None:
                return [violation]
            continue
        if top_key == "tool":
            violation = _tool_unknown_change_violation(
                path=path,
                protected_pattern=protected_pattern,
                old_value=old_value,
                new_value=new_value,
                old_text=old_text,
                new_text=new_text,
            )
            if violation is not None:
                return [violation]
            continue
        if top_key in {"build-system", "dependency-groups"}:
            continue
        if old_value != new_value:
            return [
                _violation(
                    path=path,
                    protected_pattern=protected_pattern,
                    section=top_key,
                    line=_line_for_toml_section(
                        new_text if new_value is not None else old_text, top_key
                    ),
                    reason=f"pyproject section changed outside allowed metadata/dependency edits: {top_key}",
                )
            ]
    return []


def _project_unknown_change_violation(
    *,
    path: str,
    protected_pattern: str,
    old_value: object,
    new_value: object,
    old_text: str,
    new_text: str,
) -> QualityGateViolation | None:
    if old_value is not None and not isinstance(old_value, Mapping):
        return _violation(
            path=path,
            protected_pattern=protected_pattern,
            section="project",
            line=_line_for_toml_section(old_text, "project"),
            reason="project section has unsupported format",
        )
    if new_value is not None and not isinstance(new_value, Mapping):
        return _violation(
            path=path,
            protected_pattern=protected_pattern,
            section="project",
            line=_line_for_toml_section(new_text, "project"),
            reason="project section has unsupported format",
        )
    old_project = cast(Mapping[str, object], old_value or {})
    new_project = cast(Mapping[str, object], new_value or {})
    for key in sorted(set(old_project) | set(new_project)):
        if key in _ALLOWED_PROJECT_METADATA_KEYS:
            continue
        if old_project.get(key) != new_project.get(key):
            section = f"project.{key}"
            return _violation(
                path=path,
                protected_pattern=protected_pattern,
                section=section,
                line=_line_for_toml_section(new_text, section)
                or _line_for_toml_section(old_text, section),
                reason=f"pyproject project section changed outside allowed metadata: {section}",
            )
    return None


def _tool_unknown_change_violation(
    *,
    path: str,
    protected_pattern: str,
    old_value: object,
    new_value: object,
    old_text: str,
    new_text: str,
) -> QualityGateViolation | None:
    if old_value is not None and not isinstance(old_value, Mapping):
        return _violation(
            path=path,
            protected_pattern=protected_pattern,
            section="tool",
            line=_line_for_toml_section(old_text, "tool"),
            reason="tool section has unsupported format",
        )
    if new_value is not None and not isinstance(new_value, Mapping):
        return _violation(
            path=path,
            protected_pattern=protected_pattern,
            section="tool",
            line=_line_for_toml_section(new_text, "tool"),
            reason="tool section has unsupported format",
        )
    old_tool = cast(Mapping[str, object], old_value or {})
    new_tool = cast(Mapping[str, object], new_value or {})
    policy_tool_sections = {"coverage", "hatch", "mypy", "pytest", "ruff"}
    for key in sorted(set(old_tool) | set(new_tool)):
        if key in policy_tool_sections:
            continue
        if old_tool.get(key) != new_tool.get(key):
            section = f"tool.{key}"
            return _violation(
                path=path,
                protected_pattern=protected_pattern,
                section=section,
                line=_line_for_toml_section(new_text, section)
                or _line_for_toml_section(old_text, section),
                reason=f"pyproject tool section changed outside allowed edits: {section}",
            )
    return None


def _classify_workflow_change(
    diff: ProtectedFileDiff,
    protected_pattern: str,
) -> list[QualityGateViolation]:
    path = _normalize_path(diff.path)
    if diff.old_text is None or diff.new_text is None:
        return [
            _violation(
                path=path,
                protected_pattern=protected_pattern,
                section=path,
                line=None,
                reason="could not read old and new workflow content for classification",
            )
        ]
    if diff.old_text == diff.new_text:
        return []
    old_workflow, old_error = _parse_workflow_yaml(diff.old_text, path, protected_pattern)
    if old_error is not None:
        return [old_error]
    new_workflow, new_error = _parse_workflow_yaml(diff.new_text, path, protected_pattern)
    if new_error is not None:
        return [new_error]
    assert old_workflow is not None
    assert new_workflow is not None
    old_jobs = _workflow_jobs(old_workflow)
    new_jobs = _workflow_jobs(new_workflow)
    if old_jobs is None or new_jobs is None:
        line_text = diff.new_text if new_jobs is None else diff.old_text
        return [
            _violation(
                path=path,
                protected_pattern=protected_pattern,
                section="jobs",
                line=_line_for_yaml_key(line_text, "jobs"),
                reason="workflow jobs section has unsupported format",
            )
        ]

    violations = _workflow_top_level_violations(
        path=path,
        protected_pattern=protected_pattern,
        old_workflow=old_workflow,
        new_workflow=new_workflow,
        old_text=diff.old_text,
        new_text=diff.new_text,
    )
    for job_id in sorted(set(old_jobs) - set(new_jobs)):
        violations.append(
            _violation(
                path=path,
                protected_pattern=protected_pattern,
                section=f"jobs.{job_id}",
                line=_line_for_workflow_job(diff.old_text, job_id),
                reason=f"workflow job removed: jobs.{job_id}",
            )
        )
    for job_id in sorted(set(new_jobs) - set(old_jobs)):
        new_job = new_jobs[job_id]
        if not isinstance(new_job, Mapping) or not _is_informational_job(
            job_id, cast(Mapping[str, Any], new_job)
        ):
            violations.append(
                _violation(
                    path=path,
                    protected_pattern=protected_pattern,
                    section=f"jobs.{job_id}",
                    line=_line_for_workflow_job(diff.new_text, job_id),
                    reason=(
                        "added workflow jobs must be informational/comment/notify only: "
                        f"jobs.{job_id}"
                    ),
                )
            )
    for job_id in sorted(set(old_jobs) & set(new_jobs)):
        old_job = old_jobs[job_id]
        new_job = new_jobs[job_id]
        if not isinstance(old_job, Mapping) or not isinstance(new_job, Mapping):
            violations.append(
                _violation(
                    path=path,
                    protected_pattern=protected_pattern,
                    section=f"jobs.{job_id}",
                    line=_line_for_workflow_job(diff.new_text, job_id),
                    reason=f"workflow job has unsupported format: jobs.{job_id}",
                )
            )
            continue
        violations.extend(
            _workflow_existing_job_violations(
                path=path,
                protected_pattern=protected_pattern,
                job_id=job_id,
                old_job=cast(Mapping[str, Any], old_job),
                new_job=cast(Mapping[str, Any], new_job),
                old_text=diff.old_text,
                new_text=diff.new_text,
            )
        )
    return violations


def _parse_workflow_yaml(
    text: str,
    path: str,
    protected_pattern: str,
) -> tuple[Mapping[str, Any] | None, QualityGateViolation | None]:
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        line = None
        mark = getattr(exc, "problem_mark", None)
        if mark is not None and isinstance(getattr(mark, "line", None), int):
            line = int(mark.line) + 1
        return None, _violation(
            path=path,
            protected_pattern=protected_pattern,
            section=path,
            line=line,
            reason=f"could not parse workflow YAML safely: {exc}",
        )
    if loaded is None:
        return {}, None
    if not isinstance(loaded, Mapping):
        return None, _violation(
            path=path,
            protected_pattern=protected_pattern,
            section=path,
            line=None,
            reason="workflow YAML root has unsupported format",
        )
    return cast(Mapping[str, Any], loaded), None


def _workflow_jobs(workflow: Mapping[str, Any]) -> Mapping[str, object] | None:
    jobs = workflow.get("jobs")
    if jobs is None:
        return {}
    if not isinstance(jobs, Mapping):
        return None
    return cast(Mapping[str, object], jobs)


def _workflow_top_level_violations(
    *,
    path: str,
    protected_pattern: str,
    old_workflow: Mapping[str, Any],
    new_workflow: Mapping[str, Any],
    old_text: str,
    new_text: str,
) -> list[QualityGateViolation]:
    old_fields = _workflow_top_level_fields(old_workflow, old_text)
    new_fields = _workflow_top_level_fields(new_workflow, new_text)
    violations: list[QualityGateViolation] = []
    sentinel = object()
    for field in sorted((set(old_fields) | set(new_fields)) - {"jobs"}):
        old_value = old_fields.get(field, sentinel)
        new_value = new_fields.get(field, sentinel)
        if old_value == new_value:
            continue
        line_text = new_text if field in new_fields else old_text
        violations.append(
            _violation(
                path=path,
                protected_pattern=protected_pattern,
                section=f"workflow.{field}",
                line=_line_for_yaml_top_level_key(line_text, field),
                reason=f"workflow top-level field changed outside allowed cases: {field}",
            )
        )
    return violations


def _workflow_top_level_fields(
    workflow: Mapping[str, Any],
    text: str,
) -> dict[str, object]:
    return {_workflow_top_level_field_name(key, text): value for key, value in workflow.items()}


def _workflow_top_level_field_name(key: object, text: str) -> str:
    if key is True and _line_for_yaml_top_level_key(text, "on") is not None:
        return "on"
    return str(key)


def _workflow_existing_job_violations(
    *,
    path: str,
    protected_pattern: str,
    job_id: str,
    old_job: Mapping[str, Any],
    new_job: Mapping[str, Any],
    old_text: str,
    new_text: str,
) -> list[QualityGateViolation]:
    violations: list[QualityGateViolation] = []
    if old_job.get("if") != new_job.get("if"):
        violations.append(
            _violation(
                path=path,
                protected_pattern=protected_pattern,
                section=f"jobs.{job_id}.if",
                line=_line_for_workflow_job(new_text, job_id),
                reason=f"workflow gate if changed: jobs.{job_id}.if",
            )
        )
    old_steps = _workflow_steps(old_job)
    new_steps = _workflow_steps(new_job)
    if old_steps is None or new_steps is None:
        violations.append(
            _violation(
                path=path,
                protected_pattern=protected_pattern,
                section=f"jobs.{job_id}.steps",
                line=_line_for_workflow_job(new_text, job_id),
                reason=f"workflow steps have unsupported format: jobs.{job_id}.steps",
            )
        )
        return violations

    matched_new_indexes: set[int] = set()
    for old_step in old_steps:
        match_index = _matching_step_index(old_step, new_steps, matched_new_indexes)
        old_label = _step_label(old_step)
        if match_index is None:
            violations.append(
                _violation(
                    path=path,
                    protected_pattern=protected_pattern,
                    section=f"jobs.{job_id}.steps.{old_label}",
                    line=_line_for_workflow_step(old_text, old_step),
                    reason=f"workflow step removed: jobs.{job_id}.steps.{old_label}",
                )
            )
            continue
        matched_new_indexes.add(match_index)
        violations.extend(
            _workflow_existing_step_violations(
                path=path,
                protected_pattern=protected_pattern,
                job_id=job_id,
                old_step=old_step,
                new_step=new_steps[match_index],
                new_text=new_text,
            )
        )

    for index, new_step in enumerate(new_steps):
        if index in matched_new_indexes:
            continue
        label = _step_label(new_step)
        if not _is_informational_step(new_step):
            violations.append(
                _violation(
                    path=path,
                    protected_pattern=protected_pattern,
                    section=f"jobs.{job_id}.steps.{label}",
                    line=_line_for_workflow_step(new_text, new_step),
                    reason=(
                        "added workflow steps must be informational/comment/notify only: "
                        f"jobs.{job_id}.steps.{label}"
                    ),
                )
            )

    old_job_remainder = {key: value for key, value in old_job.items() if key not in {"if", "steps"}}
    new_job_remainder = {key: value for key, value in new_job.items() if key not in {"if", "steps"}}
    if old_job_remainder != new_job_remainder:
        violations.append(
            _violation(
                path=path,
                protected_pattern=protected_pattern,
                section=f"jobs.{job_id}",
                line=_line_for_workflow_job(new_text, job_id),
                reason=f"workflow job changed outside allowed fields: jobs.{job_id}",
            )
        )
    return violations


def _workflow_existing_step_violations(
    *,
    path: str,
    protected_pattern: str,
    job_id: str,
    old_step: Mapping[str, Any],
    new_step: Mapping[str, Any],
    new_text: str,
) -> list[QualityGateViolation]:
    violations: list[QualityGateViolation] = []
    label = _step_label(new_step)
    section_prefix = f"jobs.{job_id}.steps.{label}"

    if old_step.get("if") != new_step.get("if"):
        violations.append(
            _violation(
                path=path,
                protected_pattern=protected_pattern,
                section=f"{section_prefix}.if",
                line=_line_for_workflow_step(new_text, new_step),
                reason=f"workflow gate if changed: {section_prefix}.if",
            )
        )

    old_continue = old_step.get("continue-on-error")
    new_continue = new_step.get("continue-on-error")
    if _is_true(new_continue) and not _is_true(old_continue):
        if not _is_comment_or_notify_step(new_step):
            violations.append(
                _violation(
                    path=path,
                    protected_pattern=protected_pattern,
                    section=f"{section_prefix}.continue-on-error",
                    line=_line_for_workflow_step_key(new_text, new_step, key="continue-on-error"),
                    reason="continue-on-error is only allowed for comment/notify steps",
                )
            )
    elif old_continue != new_continue:
        violations.append(
            _violation(
                path=path,
                protected_pattern=protected_pattern,
                section=f"{section_prefix}.continue-on-error",
                line=_line_for_workflow_step_key(new_text, new_step, key="continue-on-error"),
                reason=f"workflow continue-on-error changed outside allowed comment steps: {section_prefix}",
            )
        )

    old_uses = _string_value(old_step.get("uses"))
    new_uses = _string_value(new_step.get("uses"))
    if old_uses != new_uses:
        is_allowed_pinned_bump = (
            old_uses is not None
            and new_uses is not None
            and _is_pinned_uses_bump(old_uses, new_uses)
        )
        if not is_allowed_pinned_bump:
            violations.append(
                _violation(
                    path=path,
                    protected_pattern=protected_pattern,
                    section=f"{section_prefix}.uses",
                    line=_line_for_workflow_step_key(new_text, new_step, key="uses"),
                    reason=f"workflow action changed outside pinned ref bump: {section_prefix}.uses",
                )
            )

    old_run = _string_value(old_step.get("run"))
    new_run = _string_value(new_step.get("run"))
    if old_run != new_run:
        if not _is_validation_command(old_run) and _is_validation_command(new_run):
            violations.append(
                _violation(
                    path=path,
                    protected_pattern=protected_pattern,
                    section=f"{section_prefix}.run",
                    line=_line_for_workflow_step_key(new_text, new_step, key="run"),
                    reason="workflow validation command changed; test-command narrowing is blocked",
                )
            )
        elif not _is_comment_or_notify_step(new_step):
            violations.append(
                _violation(
                    path=path,
                    protected_pattern=protected_pattern,
                    section=f"{section_prefix}.run",
                    line=_line_for_workflow_step_key(new_text, new_step, key="run"),
                    reason=f"workflow run command changed outside informational step: {section_prefix}.run",
                )
            )

    if _step_remainder(old_step) != _step_remainder(new_step):
        violations.append(
            _violation(
                path=path,
                protected_pattern=protected_pattern,
                section=section_prefix,
                line=_line_for_workflow_step(new_text, new_step),
                reason=f"workflow step changed outside allowed fields: {section_prefix}",
            )
        )
    return violations


def _workflow_steps(job: Mapping[str, Any]) -> list[Mapping[str, Any]] | None:
    raw_steps = job.get("steps")
    if raw_steps is None:
        return []
    if not isinstance(raw_steps, list):
        return None
    steps: list[Mapping[str, Any]] = []
    for step in raw_steps:
        if not isinstance(step, Mapping):
            return None
        steps.append(cast(Mapping[str, Any], step))
    return steps


def _matching_step_index(
    old_step: Mapping[str, Any],
    new_steps: Sequence[Mapping[str, Any]],
    used_indexes: set[int],
) -> int | None:
    identity = _step_identity(old_step)
    if identity is None:
        return None
    for index, step in enumerate(new_steps):
        if index in used_indexes:
            continue
        if _step_identity(step) == identity:
            return index
    return None


def _step_identity(step: Mapping[str, Any]) -> tuple[str, str] | None:
    step_id = _string_value(step.get("id"))
    if step_id:
        return ("id", step_id)
    name = _string_value(step.get("name"))
    if name:
        return ("name", name)
    uses = _string_value(step.get("uses"))
    if uses:
        action = _uses_action(uses)
        return ("uses", action or uses)
    run = _string_value(step.get("run"))
    if run:
        return ("run", run)
    return None


def _step_label(step: Mapping[str, Any]) -> str:
    for key in ("name", "id", "uses", "run"):
        value = _string_value(step.get(key))
        if value:
            return value.strip().replace("\n", " ")[:80]
    return "unknown"


def _is_comment_or_notify_step(step: Mapping[str, Any]) -> bool:
    label_parts = [
        value for key in ("id", "name") if (value := _string_value(step.get(key))) is not None
    ]
    uses = _string_value(step.get("uses"))
    if uses is not None:
        label_parts.append(_uses_action(uses) or uses)
    label = " ".join(label_parts).lower()
    return any(marker in label for marker in _COMMENT_STEP_MARKERS)


def _is_informational_job(job_id: str, job: Mapping[str, Any]) -> bool:
    if _string_value(job.get("uses")) is not None:
        return False
    label_parts = [job_id]
    name = _string_value(job.get("name"))
    if name:
        label_parts.append(name)
    label = " ".join(label_parts).lower()
    if not any(marker in label for marker in _INFORMATIONAL_MARKERS):
        return False
    steps = _workflow_steps(job)
    if steps is None:
        return False
    return bool(steps) and all(_is_informational_step(step) for step in steps)


def _is_informational_step(step: Mapping[str, Any]) -> bool:
    if _string_value(step.get("uses")) is not None:
        return False
    if not _is_comment_or_notify_step(step):
        label = _step_label(step).lower()
        if not any(marker in label for marker in _INFORMATIONAL_MARKERS):
            return False
    run = _string_value(step.get("run"))
    return not _is_validation_command(run)


def _is_validation_command(command: str | None) -> bool:
    if command is None:
        return False
    normalized = command.lower()
    return (
        "tests/" in normalized
        or _VALIDATION_COMMAND_TOKEN_RE.search(normalized) is not None
        or _VALIDATION_TEST_COMMAND_RE.search(normalized) is not None
        or _VALIDATION_UNITTEST_COMMAND_RE.search(normalized) is not None
    )


def _is_pinned_uses_bump(old_uses: str, new_uses: str) -> bool:
    old_parts = _uses_action_and_ref(old_uses)
    new_parts = _uses_action_and_ref(new_uses)
    if old_parts is None or new_parts is None:
        return False
    old_action, old_ref = old_parts
    new_action, new_ref = new_parts
    if old_action != new_action or old_ref == new_ref:
        return False
    if not _is_pinned_workflow_uses_ref(old_ref) or not _is_pinned_workflow_uses_ref(new_ref):
        return False
    old_is_sha = _PINNED_WORKFLOW_USES_SHA_RE.fullmatch(old_ref) is not None
    new_is_sha = _PINNED_WORKFLOW_USES_SHA_RE.fullmatch(new_ref) is not None
    if old_is_sha or new_is_sha:
        return True
    return _is_workflow_version_ref_non_downgrade(old_ref, new_ref)


def _is_workflow_version_ref_non_downgrade(old_ref: str, new_ref: str) -> bool:
    old_key = _workflow_version_ref_sort_key(old_ref)
    new_key = _workflow_version_ref_sort_key(new_ref)
    if old_key is None or new_key is None:
        return False
    return new_key >= old_key


def _workflow_version_ref_sort_key(ref: str) -> tuple[tuple[int, ...], int, str] | None:
    if _PINNED_WORKFLOW_USES_VERSION_RE.fullmatch(ref) is None:
        return None
    raw_version = ref[1:] if ref.startswith(("v", "V")) else ref
    version_without_build = raw_version.split("+", 1)[0]
    core, separator, prerelease = version_without_build.partition("-")
    numbers = [int(part) for part in core.split(".")]
    while len(numbers) < 3:
        numbers.append(0)
    release_rank = 0 if separator else 1
    return tuple(numbers[:3]), release_rank, prerelease


def _uses_action(value: str) -> str | None:
    parts = _uses_action_and_ref(value)
    if parts is None:
        return None
    action, _ref = parts
    return action


def _uses_action_and_ref(value: str) -> tuple[str, str] | None:
    action, separator, ref = value.strip().partition("@")
    if not separator or not action or not ref:
        return None
    return action, ref


def _is_pinned_workflow_uses_ref(ref: str) -> bool:
    return (
        _PINNED_WORKFLOW_USES_SHA_RE.fullmatch(ref) is not None
        or _PINNED_WORKFLOW_USES_VERSION_RE.fullmatch(ref) is not None
    )


def _step_remainder(
    step: Mapping[str, Any],
) -> dict[str, object]:
    ignored = {"continue-on-error", "if", "run", "uses"}
    return {key: value for key, value in step.items() if key not in ignored}


def _nested_value(data: Mapping[str, Any], keys: tuple[str, ...]) -> object:
    current: object = data
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _is_true(value: object) -> bool:
    return value is True or (isinstance(value, str) and value.lower() == "true")


def _string_value(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _format_number(value: float) -> str:
    return str(int(value)) if value.is_integer() else f"{value:g}"


def _line_for_toml_section(text: str, section: str) -> int | None:
    header = f"[{section}]"
    return _line_containing(text, header)


def _line_for_toml_key(text: str, *, section: str, key: str) -> int | None:
    lines = text.splitlines()
    in_section = False
    for index, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_section = stripped == f"[{section}]"
            continue
        if in_section and re.match(rf"^{re.escape(key)}\s*=", stripped):
            return index
    return _line_for_toml_section(text, section)


def _line_for_yaml_key(text: str, key: str) -> int | None:
    return _line_matching(text, rf"^\s*{re.escape(key)}\s*:")


def _line_for_yaml_top_level_key(text: str, key: str) -> int | None:
    return _line_matching(text, rf"^{re.escape(key)}\s*:")


def _line_for_workflow_job(text: str, job_id: str) -> int | None:
    return _line_matching(text, rf"^\s{{2,}}{re.escape(job_id)}\s*:")


def _line_for_workflow_step(text: str, step: Mapping[str, Any]) -> int | None:
    for key in ("name", "id", "uses", "run"):
        value = _string_value(step.get(key))
        if value:
            line = _line_containing(text, f"{key}: {value}")
            if line is not None:
                return line
    return None


def _line_for_workflow_step_key(
    text: str,
    step: Mapping[str, Any],
    *,
    key: str,
) -> int | None:
    step_line = _line_for_workflow_step(text, step)
    lines = text.splitlines()
    if step_line is not None:
        for index in range(step_line, min(len(lines), step_line + 12) + 1):
            if re.match(rf"^\s*{re.escape(key)}\s*:", lines[index - 1]):
                return index
    return _line_matching(text, rf"^\s*{re.escape(key)}\s*:")


def _line_containing(text: str, needle: str) -> int | None:
    for index, line in enumerate(text.splitlines(), start=1):
        if needle in line:
            return index
    return None


def _line_matching(text: str, pattern: str) -> int | None:
    compiled = re.compile(pattern)
    for index, line in enumerate(text.splitlines(), start=1):
        if compiled.match(line):
            return index
    return None


def _matched_protected_pattern(path: str) -> str | None:
    for pattern in PROTECTED_QUALITY_GATE_PATHS:
        normalized_pattern = _normalize_path(pattern)
        if normalized_pattern.endswith("/"):
            if path.startswith(normalized_pattern):
                return pattern
        elif path == normalized_pattern:
            return pattern
    return None


def _is_workflow_yaml_path(path: str) -> bool:
    return path.startswith(_WORKFLOW_PREFIX) and path.endswith((".yml", ".yaml"))


def _is_owned(path: str, owned_paths: list[str] | tuple[str, ...]) -> bool:
    return any(owned_paths_overlap(path, owned_path) for owned_path in owned_paths)


def _normalize_path(path: str) -> str:
    normalized = path.strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized
