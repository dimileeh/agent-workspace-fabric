"""Quality-gate protections for agent-authored workspace changes."""

from __future__ import annotations

import re
import tomllib
from collections import Counter
from collections.abc import Mapping
from typing import Any, Final, cast

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
    r"(?:\s+(?:run|exec|--?[A-Za-z0-9_.=:/-]+))*"
    r"\s+test(?:\s|$)"
)
_VALIDATION_UNITTEST_COMMAND_RE: Final = re.compile(
    r"(?<![A-Za-z0-9_./-])python(?:3(?:\.\d+)?)?\s+-m\s+unittest(?:\s|$)"
)
_VALIDATION_TEST_PATH_RE: Final = re.compile(
    r"(?<![A-Za-z0-9_./-])"
    r"(?:(?:uv|poetry|pipenv)\s+run\s+)?python(?:3(?:\.\d+)?)?"
    r"(?:\s+--?[A-Za-z0-9_.=:/-]+)*"
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


from awf.control.quality_gates_common import (  # noqa: E402
    ProtectedFileDiff,
    QualityGateViolation,
    _format_number,
    _format_toml_policy_value,
    _is_number,
    _line_containing,
    _line_matching,
    _nested_value,
    _normalize_path,
    _violation,
)


def _classify_pyproject_change(
    diff: ProtectedFileDiff,
    protected_pattern: str,
) -> list[QualityGateViolation]:
    path = _normalize_path(diff.path)
    if diff.old_text is None or diff.new_text is None:
        reason = _absent_protected_file_content_reason(
            old_text=diff.old_text,
            new_text=diff.new_text,
            added_reason="new pyproject.toml file added outside declared owned_paths",
            deleted_reason="pyproject.toml deleted outside declared owned_paths",
            unavailable_reason=(
                "could not read old and new pyproject.toml content for classification"
            ),
        )
        return [
            _violation(
                path=path,
                protected_pattern=protected_pattern,
                section="pyproject.toml",
                line=None,
                reason=reason,
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
                    reason = (
                        "coverage fail_under lowered from "
                        f"{_format_number(old_number)} to {_format_number(new_number)}"
                    )
                elif new_number > old_number:
                    reason = (
                        "coverage fail_under raised from "
                        f"{_format_number(old_number)} to {_format_number(new_number)} "
                        "(policy change requires ownership of pyproject.toml)"
                    )
                else:
                    reason = (
                        "coverage fail_under unchanged at "
                        f"{_format_number(new_number)} while coverage policy changed"
                    )
                    violations.append(
                        _violation(
                            path=path,
                            protected_pattern=protected_pattern,
                            section="tool.coverage",
                            line=_line_for_toml_section_or_descendant(
                                new_text,
                                "tool.coverage",
                            ),
                            reason=reason,
                        )
                    )
                    continue
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
                        reason=reason,
                    )
                )
                if _coverage_policy_without_fail_under(old_value) == (
                    _coverage_policy_without_fail_under(new_value)
                ):
                    continue
                violations.append(
                    _coverage_policy_section_violation(
                        path=path,
                        protected_pattern=protected_pattern,
                        new_text=new_text,
                    )
                )
                continue
            if old_fail_under != new_fail_under:
                reason = _coverage_fail_under_non_numeric_change_reason(
                    old_fail_under,
                    new_fail_under,
                )
                line_text = new_text if new_fail_under is not None else old_text
                violations.append(
                    _violation(
                        path=path,
                        protected_pattern=protected_pattern,
                        section="tool.coverage.report.fail_under",
                        line=_line_for_toml_key(
                            line_text,
                            section="tool.coverage.report",
                            key="fail_under",
                        ),
                        reason=reason,
                    )
                )
                if _coverage_policy_without_fail_under(old_value) == (
                    _coverage_policy_without_fail_under(new_value)
                ):
                    continue
                violations.append(
                    _coverage_policy_section_violation(
                        path=path,
                        protected_pattern=protected_pattern,
                        new_text=new_text,
                    )
                )
                continue
        line_text = new_text if new_value is not None else old_text
        violations.append(
            _violation(
                path=path,
                protected_pattern=protected_pattern,
                section=section,
                line=(
                    _line_for_toml_section_or_descendant(line_text, section)
                    if section_keys == ("tool", "coverage")
                    else _line_for_toml_section(line_text, section)
                ),
                reason=f"protected pyproject policy section changed: {section}",
            )
        )
    return violations


def _coverage_fail_under_non_numeric_change_reason(
    old_fail_under: Any,
    new_fail_under: Any,
) -> str:
    policy_suffix = "(policy change requires ownership of pyproject.toml)"
    if old_fail_under is None:
        return (
            "coverage fail_under added at "
            f"{_format_toml_policy_value(new_fail_under)} {policy_suffix}"
        )
    if new_fail_under is None:
        return (
            "coverage fail_under removed from "
            f"{_format_toml_policy_value(old_fail_under)} {policy_suffix}"
        )
    return (
        "coverage fail_under changed from "
        f"{_format_toml_policy_value(old_fail_under)} to "
        f"{_format_toml_policy_value(new_fail_under)} "
        "(fail_under must remain numeric; policy change requires ownership of pyproject.toml)"
    )


def _coverage_policy_section_violation(
    *,
    path: str,
    protected_pattern: str,
    new_text: str,
) -> QualityGateViolation:
    return _violation(
        path=path,
        protected_pattern=protected_pattern,
        section="tool.coverage",
        line=_line_for_toml_section_or_descendant(new_text, "tool.coverage"),
        reason="protected pyproject policy section changed: tool.coverage",
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


def _coverage_policy_without_fail_under(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return value
    coverage = dict(value)
    report = coverage.get("report")
    if isinstance(report, Mapping):
        report_without_fail_under = dict(report)
        report_without_fail_under.pop("fail_under", None)
        if report_without_fail_under:
            coverage["report"] = report_without_fail_under
        else:
            coverage.pop("report", None)
    return coverage


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
            allow_new_groups=True,
        )
    )
    old_dep_groups = _nested_value(old_doc, ("dependency-groups",))
    violations.extend(
        _dependency_group_violations(
            path=path,
            protected_pattern=protected_pattern,
            section_prefix="dependency-groups",
            old_value=old_dep_groups,
            new_value=_nested_value(new_doc, ("dependency-groups",)),
            old_text=old_text,
            new_text=new_text,
            allow_new_groups=old_dep_groups is None,
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
    allow_new_groups: bool,
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
        if old_groups[group] == new_groups[group]:
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
        new_dependencies = _dependency_entries(new_groups[group])
        if new_dependencies is None:
            violations.append(
                _violation(
                    path=path,
                    protected_pattern=protected_pattern,
                    section=section,
                    line=_line_for_toml_section(new_text, section),
                    reason=_dependency_group_entry_unsupported_reason(
                        section=section,
                        value=new_groups[group],
                    ),
                )
            )
            continue
        if not allow_new_groups:
            violations.append(
                _violation(
                    path=path,
                    protected_pattern=protected_pattern,
                    section=section,
                    line=_line_for_toml_key(new_text, section=section_prefix, key=group)
                    or _line_for_toml_section(new_text, section),
                    reason=f"dependency group added: {section}",
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
    if old_value == new_value:
        return []
    old_dependencies = _dependency_entries(old_value)
    new_dependencies = _dependency_entries(new_value)
    if old_dependencies is None or new_dependencies is None:
        line_text = new_text if new_dependencies is None else old_text
        unsupported_value = new_value if new_dependencies is None else old_value
        return [
            _violation(
                path=path,
                protected_pattern=protected_pattern,
                section=section,
                line=_line_for_toml_section(line_text, section),
                reason=_dependency_list_unsupported_reason(
                    section=section,
                    value=unsupported_value,
                ),
            )
        ]
    violations: list[QualityGateViolation] = []
    for name, old_entries in old_dependencies.items():
        new_entries = new_dependencies.get(name)
        if new_entries is None:
            old_raw = next(iter(old_entries))
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
        for old_raw, old_count in old_entries.items():
            if new_entries[old_raw] >= old_count:
                continue
            if _dependency_entry_count(new_entries) < _dependency_entry_count(old_entries):
                violations.append(
                    _violation(
                        path=path,
                        protected_pattern=protected_pattern,
                        section=section,
                        line=_line_containing(old_text, old_raw),
                        reason=f"dependency removed: {name}",
                    )
                )
                break
            violations.append(
                _violation(
                    path=path,
                    protected_pattern=protected_pattern,
                    section=section,
                    line=_line_containing(
                        new_text,
                        _replacement_dependency_raw(
                            old_entries=old_entries,
                            new_entries=new_entries,
                        ),
                    )
                    or _line_containing(old_text, old_raw),
                    reason=f"dependency changed: {name}",
                )
            )
            break
    return violations


def _dependency_entries(value: object) -> dict[str, Counter[str]] | None:
    if value is None:
        return {}
    if not isinstance(value, list):
        return None
    dependencies: dict[str, Counter[str]] = {}
    for item in value:
        if not isinstance(item, str):
            return None
        name = _dependency_name(item)
        if name is None:
            return None
        dependencies.setdefault(name, Counter())[item] += 1
    return dependencies


def _dependency_list_unsupported_reason(*, section: str, value: object) -> str:
    if _contains_pep735_include_group(value):
        return (
            "dependency section contains PEP 735 include-group entries that "
            f"require ownership of pyproject.toml for evaluation: {section}"
        )
    return f"dependency section has unsupported format: {section}"


def _dependency_group_entry_unsupported_reason(*, section: str, value: object) -> str:
    if _contains_pep735_include_group(value):
        return (
            "dependency section contains PEP 735 include-group entries that "
            f"require ownership of pyproject.toml for evaluation: {section}"
        )
    return f"dependency group has unsupported format: {section}"


def _contains_pep735_include_group(value: object) -> bool:
    if not isinstance(value, list):
        return False
    return any(isinstance(item, Mapping) and "include-group" in item for item in value)


def _dependency_entry_count(entries: Counter[str]) -> int:
    return sum(entries.values())


def _replacement_dependency_raw(
    *,
    old_entries: Counter[str],
    new_entries: Counter[str],
) -> str:
    for new_raw, new_count in new_entries.items():
        if new_count > old_entries[new_raw]:
            return new_raw
    return next(iter(new_entries))


def _dependency_name(requirement: str) -> str | None:
    match = re.match(r"\s*([A-Za-z0-9_.-]+)", requirement)
    if match is None:
        return None
    return re.sub(r"[-_.]+", "-", match.group(1)).lower()


def _pyproject_unknown_change_violations(
    *,
    path: str,
    protected_pattern: str,
    old_doc: Mapping[str, Any],
    new_doc: Mapping[str, Any],
    old_text: str,
    new_text: str,
) -> list[QualityGateViolation]:
    violations: list[QualityGateViolation] = []
    for top_key in sorted(set(old_doc) | set(new_doc)):
        old_value = old_doc.get(top_key)
        new_value = new_doc.get(top_key)
        if top_key == "project":
            violations.extend(
                _project_unknown_change_violations(
                    path=path,
                    protected_pattern=protected_pattern,
                    old_value=old_value,
                    new_value=new_value,
                    old_text=old_text,
                    new_text=new_text,
                )
            )
            continue
        if top_key == "tool":
            violations.extend(
                _tool_unknown_change_violations(
                    path=path,
                    protected_pattern=protected_pattern,
                    old_value=old_value,
                    new_value=new_value,
                    old_text=old_text,
                    new_text=new_text,
                )
            )
            continue
        if top_key in {"build-system", "dependency-groups"}:
            continue
        if old_value != new_value:
            violations.append(
                _violation(
                    path=path,
                    protected_pattern=protected_pattern,
                    section=top_key,
                    line=_line_for_toml_section(
                        new_text if new_value is not None else old_text, top_key
                    ),
                    reason=f"pyproject section changed outside allowed metadata/dependency edits: {top_key}",
                )
            )
    return violations


def _project_unknown_change_violations(
    *,
    path: str,
    protected_pattern: str,
    old_value: object,
    new_value: object,
    old_text: str,
    new_text: str,
) -> list[QualityGateViolation]:
    if old_value is not None and not isinstance(old_value, Mapping):
        return [
            _violation(
                path=path,
                protected_pattern=protected_pattern,
                section="project",
                line=_line_for_toml_section(old_text, "project"),
                reason="project section has unsupported format",
            )
        ]
    if new_value is not None and not isinstance(new_value, Mapping):
        return [
            _violation(
                path=path,
                protected_pattern=protected_pattern,
                section="project",
                line=_line_for_toml_section(new_text, "project"),
                reason="project section has unsupported format",
            )
        ]
    old_project = cast(Mapping[str, object], old_value or {})
    new_project = cast(Mapping[str, object], new_value or {})
    violations: list[QualityGateViolation] = []
    for key in sorted(set(old_project) | set(new_project)):
        if key in _ALLOWED_PROJECT_METADATA_KEYS:
            continue
        if old_project.get(key) != new_project.get(key):
            section = f"project.{key}"
            violations.append(
                _violation(
                    path=path,
                    protected_pattern=protected_pattern,
                    section=section,
                    line=_line_for_toml_section(new_text, section)
                    or _line_for_toml_section(old_text, section),
                    reason=f"pyproject project section changed outside allowed metadata: {section}",
                )
            )
    return violations


def _tool_unknown_change_violations(
    *,
    path: str,
    protected_pattern: str,
    old_value: object,
    new_value: object,
    old_text: str,
    new_text: str,
) -> list[QualityGateViolation]:
    if old_value is not None and not isinstance(old_value, Mapping):
        return [
            _violation(
                path=path,
                protected_pattern=protected_pattern,
                section="tool",
                line=_line_for_toml_section(old_text, "tool"),
                reason="tool section has unsupported format",
            )
        ]
    if new_value is not None and not isinstance(new_value, Mapping):
        return [
            _violation(
                path=path,
                protected_pattern=protected_pattern,
                section="tool",
                line=_line_for_toml_section(new_text, "tool"),
                reason="tool section has unsupported format",
            )
        ]
    old_tool = cast(Mapping[str, object], old_value or {})
    new_tool = cast(Mapping[str, object], new_value or {})
    policy_tool_sections = {"coverage", "hatch", "mypy", "pytest", "ruff"}
    violations: list[QualityGateViolation] = []
    for key in sorted(set(old_tool) | set(new_tool)):
        if key in policy_tool_sections:
            continue
        if old_tool.get(key) != new_tool.get(key):
            section = f"tool.{key}"
            violations.append(
                _violation(
                    path=path,
                    protected_pattern=protected_pattern,
                    section=section,
                    line=_line_for_toml_section(new_text, section)
                    or _line_for_toml_section(old_text, section),
                    reason=f"pyproject tool section changed outside allowed edits: {section}",
                )
            )
    return violations


def _line_for_toml_section(text: str, section: str) -> int | None:
    header = f"[{section}]"
    return _line_containing(text, header)


def _line_for_toml_section_or_descendant(text: str, section: str) -> int | None:
    exact_line = _line_for_toml_section(text, section)
    if exact_line is not None:
        return exact_line
    return _line_matching(text, rf"^\s*\[{re.escape(section)}(?:\.|\])")


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
