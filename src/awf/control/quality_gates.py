"""Quality-gate protections for agent-authored workspace changes."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from awf.control.quality_gates_common import (
    ProtectedFileDiff,
    QualityGateViolation,
    _normalize_path,
    _violation,
)
from awf.db.repositories import owned_paths_overlap


@dataclass(frozen=True)
class GrantSpec:
    """An operator's scoped protected-path grant, as seen by the gate.

    Built from the active ``OperatorGrantAuditRecord`` rows for a workspace so
    this module stays free of DB imports. ``path`` is a canonicalized,
    repo-relative glob (``../`` traversal already rejected upstream at grant
    time). ``approve_policy_downgrade`` is the operator's acknowledgement that
    the granted edit may WEAKEN validation.
    """

    path: str
    approve_policy_downgrade: bool = False


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
_COMMENT_NOTIFY_ACTION_USES: Final[frozenset[str]] = frozenset(
    {
        "peter-evans/create-or-update-comment",
    }
)
_COMMENT_NOTIFY_ACTION_ALLOWED_WITH_KEYS: Final[dict[str, frozenset[str]]] = {
    "peter-evans/create-or-update-comment": frozenset(
        {
            "append-separator",
            "body",
            "comment-id",
            "edit-mode",
            "issue-number",
            "reactions",
            "reactions-edit-mode",
        }
    )
}
_COMMENT_NOTIFY_CAPABLE_ACTION_USES: Final[frozenset[str]] = frozenset(
    {
        "actions/github-script",
    }
)
_GITHUB_SCRIPT_COMMENT_ALLOWED_REST_METHODS: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("issues", "createComment"),
        ("issues", "updateComment"),
        ("pulls", "createReviewComment"),
    }
)
_GITHUB_SCRIPT_COMMENT_ALLOWED_WITH_KEYS: Final[frozenset[str]] = frozenset(
    {"debug", "result-encoding", "retries", "retry-exempt-status-codes", "script"}
)
_GITHUB_SCRIPT_REST_METHOD_RE: Final = re.compile(
    r"\bgithub\.rest\.([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\s*\("
)
_GITHUB_SCRIPT_BLOCKED_ACCESS_RE: Final = re.compile(
    r"\b(?:core|exec|glob|io)\s*\."
    r"|\b(?:eval|fetch|Function|require|setInterval|setTimeout)\s*\("
    r"|\bimport\s*\("
    r"|\bprocess\s*(?:\.|\?\.|\[)"
    r"|\b(?:github|context)\s*(?:\.|\?\.)\s*token\b"
    r"|\b(?:github|context)\s*(?:\?\.)?\[\s*(?:['\"]token['\"]|`token`)\s*\]"
    r"|\bgithub\s*\["
    r"|\bgithub\.(?:graphql|paginate|request)\s*\("
)
_SENSITIVE_WORKFLOW_WITH_INPUT_PARTS: Final[frozenset[str]] = frozenset(
    {"credential", "credentials", "password", "secret", "secrets", "token"}
)
_SENSITIVE_WORKFLOW_WITH_INPUT_NAMES: Final[frozenset[str]] = frozenset(
    {"access-key", "api-key", "client-secret", "deploy-key", "private-key", "ssh-key"}
)
_WORKFLOW_PINNED_BUMP_ALLOWED_WITH_KEYS: Final[dict[str, frozenset[str]]] = {
    "actions/cache": frozenset(
        {
            "enablecrossosarchive",
            "fail-on-cache-miss",
            "key",
            "lookup-only",
            "path",
            "restore-keys",
            "save-always",
            "upload-chunk-size",
        }
    ),
    "actions/cache/restore": frozenset(
        {
            "enablecrossosarchive",
            "fail-on-cache-miss",
            "key",
            "lookup-only",
            "path",
            "restore-keys",
        }
    ),
    "actions/cache/save": frozenset(
        {
            "enablecrossosarchive",
            "key",
            "path",
            "upload-chunk-size",
        }
    ),
    "actions/setup-python": frozenset({"python-version"}),
}
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
_INFORMATIONAL_JOB_ALLOWED_KEYS: Final[frozenset[str]] = frozenset(
    {"env", "if", "name", "needs", "permissions", "runs-on", "steps"}
)
_INFORMATIONAL_JOB_COMMENT_PERMISSION_SCOPES: Final[frozenset[str]] = frozenset(
    {"issues", "pull-requests"}
)
_INFORMATIONAL_JOB_READ_PERMISSION_SCOPES: Final[frozenset[str]] = frozenset({"contents"})
_INFORMATIONAL_STEP_ALLOWED_KEYS: Final[frozenset[str]] = frozenset(
    {"continue-on-error", "env", "id", "if", "name", "run", "uses", "with"}
)
_INFORMATIONAL_RUN_COMMAND_NAMES: Final[frozenset[str]] = frozenset({"echo", "printf"})
_INFORMATIONAL_RUN_SEPARATORS: Final[frozenset[str]] = frozenset({";", "&&"})
_INFORMATIONAL_RUN_BLOCKED_OPERATORS: Final[frozenset[str]] = frozenset(
    {"|", "|&", "||", "&", "<", ">", "<<", ">>", "<>", ">|", "<<<", "&>", "&>>", ">&", "<&"}
)
_VALIDATION_RUN_APPEND_BLOCKED_OPERATORS: Final[frozenset[str]] = (
    _INFORMATIONAL_RUN_BLOCKED_OPERATORS | frozenset({";"})
)
_VALIDATION_RUN_DIRECT_COMMAND_NAMES: Final[frozenset[str]] = frozenset({"mypy", "pytest", "ruff"})
_VALIDATION_RUN_MODULE_NAMES: Final[frozenset[str]] = (
    _VALIDATION_RUN_DIRECT_COMMAND_NAMES | frozenset({"coverage", "unittest"})
)
_VALIDATION_RUN_COVERAGE_REPORT_COMMAND_NAMES: Final[frozenset[str]] = frozenset(
    {"annotate", "html", "json", "lcov", "report", "xml"}
)
_VALIDATION_RUN_TEST_COMMAND_NAMES: Final[frozenset[str]] = frozenset(
    {"cargo", "go", "gradle", "gradlew", "make", "mvn", "nox", "tox"}
)
_VALIDATION_RUN_TARGET_NAMES: Final[frozenset[str]] = frozenset(
    {"check", "coverage", "lint", "test", "tests"}
)
_VALIDATION_COMMAND_TOKEN_RE: Final = re.compile(
    r"(?<![A-Za-z0-9_-])"
    r"(?:pytest|ruff|mypy|coverage)"
    r"(?![A-Za-z0-9_-])"
)
_BROAD_VALIDATION_COMMAND_NAMES: Final[frozenset[str]] = frozenset(
    {"build", "lint", "deploy", "publish", "release"}
)
_BROAD_VALIDATION_SCRIPT_STEMS: Final[frozenset[str]] = frozenset(
    {"build", "lint", "deploy", "publish", "release"}
)
_SHELL_SEGMENT_SPLIT_RE: Final = re.compile(r"(?:&&|\|\||;|\n)")
_SHELL_COMMAND_SEPARATORS: Final[frozenset[str]] = frozenset({";", "&&", "||", "|", "|&", "&"})
_COMMAND_PREFIX_WORDS: Final[frozenset[str]] = frozenset({"command", "sudo", "time"})
_ENV_ASSIGNMENT_RE: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")
_BRACED_SHELL_PARAMETER_RE: Final = re.compile(r"\$\{(?!\{)")
_UNBRACED_SHELL_PARAMETER_RE: Final = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")
_GITHUB_ACTIONS_EXPRESSION_RE: Final = re.compile(r"\$\{\{\s*(?P<expression>.*?)\s*\}\}")
_SAFE_INFORMATIONAL_GITHUB_ACTIONS_EXPRESSION_RE: Final = re.compile(
    r"(?:"
    r"github\.(?:sha|run_id|run_number|run_attempt|event_name|repository|server_url|actor|"
    r"triggering_actor|job|action)"
    r"|github\.event\.pull_request\.(?:number|head\.sha|base\.ref)"
    r"|steps\.[A-Za-z_][A-Za-z0-9_-]*\.(?:outcome|conclusion)"
    r"|needs\.[A-Za-z_][A-Za-z0-9_-]*\.result"
    r")"
)
_SENSITIVE_ENV_NAME_RE: Final = re.compile(
    r"(?:^|_)(?:ACCESS_KEY|API_KEY|AUTH|CREDENTIAL|PASSWD|PASSWORD|PAT|PRIVATE_KEY|SECRET|TOKEN)(?:_|$)",
    re.IGNORECASE,
)
_ENV_NAME_RE: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_BLOCKED_INFORMATIONAL_ENV_NAMES: Final[frozenset[str]] = frozenset(
    {"BASH_ENV", "ENV", "GITHUB_ENV", "GITHUB_PATH", "IFS", "PATH", "SHELL", "SHELLOPTS"}
)
_SAFE_INFORMATIONAL_RUN_ENV_NAMES: Final[frozenset[str]] = frozenset({"PATH"})
_PACKAGE_MANAGER_OPTIONS_WITH_VALUE: Final[frozenset[str]] = frozenset(
    {"--cwd", "--dir", "--filter", "--prefix", "--workspace", "-c", "-w"}
)
_COVERAGE_OPTIONS_WITH_VALUE: Final[frozenset[str]] = frozenset(
    {"--data-file", "--debug", "--rcfile"}
)
_SCRIPT_SUFFIXES: Final[tuple[str, ...]] = (
    ".bash",
    ".fish",
    ".ps1",
    ".py",
    ".sh",
    ".zsh",
)
_VALIDATION_TEST_COMMAND_RE: Final = re.compile(
    r"(?<![A-Za-z0-9_./-])"
    r"(?:npm|pnpm|yarn|bun|go|cargo|make|mvn|gradle|gradlew|tox|nox|uv|poetry|pipenv)"
    r"(?:\s+(?:run|exec|--?[A-Za-z0-9_.=:/-]++))*+"
    r"\s+test(?:\s|$)"
)
_VALIDATION_UNITTEST_COMMAND_RE: Final = re.compile(
    r"(?<![A-Za-z0-9_./-])python(?:3(?:\.\d+)?)?\s+-m\s+unittest(?:\s|$)"
)
_VALIDATION_TEST_PATH_RE: Final = re.compile(
    r"(?<![A-Za-z0-9_./-])"
    r"(?:(?:uv|poetry|pipenv)\s+run\s+)?python(?:3(?:\.\d+)?)?"
    r"(?:\s+--?[A-Za-z0-9_.=:/-]++)*+"
    r"\s+tests/[^\s;&|]*"
)
_PINNED_WORKFLOW_USES_SHA_RE: Final = re.compile(r"^[0-9a-fA-F]{40}$")
_PINNED_WORKFLOW_USES_VERSION_RE: Final = re.compile(
    r"^[vV]?(?:\d+|\d+\.\d+|\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)$"
)
_PRERELEASE_NUMERIC_SUFFIX_RE: Final = re.compile(r"^([A-Za-z]+)(\d+)$")
type _WorkflowPrereleaseIdentifierKey = tuple[int, int | str]
type _WorkflowVersionRefSortKey = tuple[
    tuple[int, ...], int, tuple[_WorkflowPrereleaseIdentifierKey, ...]
]
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


def find_protected_quality_gate_changes(
    *,
    changed_paths: list[str] | tuple[str, ...],
    owned_paths: list[str] | tuple[str, ...],
    protected_file_diffs: Mapping[str, ProtectedFileDiff] | None = None,
    operator_granted_paths: Sequence[GrantSpec] = (),
) -> list[QualityGateViolation]:
    """Return protected quality-gate changes the task did not explicitly own.

    Agents should raise coverage by adding real tests. They should not make a
    failing workspace pass by lowering coverage, test, or CI policy. When local
    old/new file content is available, pyproject and workflow changes are
    classified semantically so explicitly safe edits can proceed. Missing or
    unparseable classifier input still fails closed.

    An operator can resolve a ``blocked`` workspace by granting specific paths
    (``operator_granted_paths``). All grant honoring lives INSIDE this one
    function so every caller is covered with no hand-union. A benign edit (the
    classifier returns no violations) passes with or without a grant. A real
    violation — a classifier-detected WEAKENING edit, a classifier-less protected
    file, or an unclassifiable change with no diff — is suppressed only when a
    matching grant carries ``approve_policy_downgrade`` (fail closed): a
    non-acknowledged grant can never silently weaken validation.
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
        path_violations = _protected_path_violations(path, protected, diff_by_path)
        if not path_violations:
            # Benign / explicitly-safe edit: nothing to suppress.
            continue
        if _grant_with_policy_downgrade_matches(path, operator_granted_paths):
            # An operator acknowledged this validation-weakening edit.
            continue
        violations.extend(path_violations)
    return violations


def _protected_path_violations(
    path: str,
    protected: str,
    diff_by_path: Mapping[str, ProtectedFileDiff],
) -> list[QualityGateViolation]:
    """Classify a single unowned protected path into zero or more violations.

    Returns an empty list for a benign classifier-backed edit; a non-empty list
    for a weakening edit, a classifier-less protected file, or an unclassifiable
    change whose diff is unavailable (fail closed).
    """
    if path == "pyproject.toml":
        diff = diff_by_path.get(path)
        if diff is None:
            return [
                _violation(
                    path=path,
                    protected_pattern=protected,
                    section=path,
                    line=None,
                    reason="diff unavailable for protected pyproject.toml change",
                )
            ]
        return _classify_pyproject_change(diff, protected)
    if _is_workflow_yaml_path(path):
        diff = diff_by_path.get(path)
        if diff is None:
            return [
                _violation(
                    path=path,
                    protected_pattern=protected,
                    section=path,
                    line=None,
                    reason="diff unavailable for protected workflow change",
                )
            ]
        return _classify_workflow_change(diff, protected)
    return [
        _violation(
            path=path,
            protected_pattern=protected,
            section=path,
            line=None,
            reason="protected quality-gate file changed outside declared owned_paths",
        )
    ]


def _grant_matches(path: str, grant_path: str) -> bool:
    """Return whether a grant glob covers a changed path.

    Reuses the owned-path overlap matcher so grant globs share the exact
    directory/wildcard semantics as ``owned_paths`` (e.g. a directory-wide
    ``.github/workflows/`` grant covers nested workflow files)."""
    normalized_grant = _normalize_path(grant_path)
    return bool(normalized_grant) and owned_paths_overlap(path, normalized_grant)


def _grant_with_policy_downgrade_matches(
    path: str, operator_granted_paths: Sequence[GrantSpec]
) -> bool:
    """Return whether an acknowledged (``approve_policy_downgrade``) grant covers ``path``."""
    return any(
        grant.approve_policy_downgrade and _grant_matches(path, grant.path)
        for grant in operator_granted_paths
    )


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


def diff_classified_protected_paths(
    changed_paths: Sequence[str],
    *,
    owned_paths: Sequence[str] = (),
) -> tuple[str, ...]:
    """Return unowned protected paths that require protected diff classification."""
    paths: list[str] = []
    for raw_path in changed_paths:
        path = _normalize_path(raw_path)
        if path and requires_protected_file_diff(path) and not _is_owned(path, owned_paths):
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


def _format_violation_detail(violation: QualityGateViolation) -> str:
    section = violation.section or violation.protected_pattern
    line = f"line {violation.line}" if violation.line is not None else "line unknown"
    return f"{violation.path} :: {section} :: {line} :: {violation.reason}"


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


def _is_owned(path: str, owned_paths: Sequence[str]) -> bool:
    return any(owned_paths_overlap(path, owned_path) for owned_path in owned_paths)


from awf.control.quality_gates_pyproject import (  # noqa: E402
    _classify_pyproject_change,
)
from awf.control.quality_gates_workflow import (  # noqa: E402
    _classify_workflow_change,
)

__all__ = [
    "GrantSpec",
    "ProtectedFileDiff",
    "QualityGateViolation",
    "find_protected_quality_gate_changes",
    "quality_gate_violation_message",
    "quality_gate_violation_details",
    "protected_quality_gate_pattern",
    "requires_protected_file_diff",
    "diff_classified_protected_paths",
    "changed_paths_are_only_internal_plan_artifacts",
    "plan_only_output_message",
]
