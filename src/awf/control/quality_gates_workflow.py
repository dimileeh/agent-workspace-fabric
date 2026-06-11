"""Quality-gate protections for agent-authored workspace changes."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from functools import lru_cache
from typing import Any, Final, cast

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

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


from awf.control.quality_gates_common import (  # noqa: E402
    ProtectedFileDiff,
    QualityGateViolation,
    _absent_protected_file_content_reason,
    _normalize_path,
    _violation,
)


def _classify_workflow_change(
    diff: ProtectedFileDiff,
    protected_pattern: str,
) -> list[QualityGateViolation]:
    path = _normalize_path(diff.path)
    if diff.old_text is None or diff.new_text is None:
        reason = _absent_protected_file_content_reason(
            old_text=diff.old_text,
            new_text=diff.new_text,
            added_reason="new workflow file added outside declared owned_paths",
            deleted_reason="workflow file deleted outside declared owned_paths",
            unavailable_reason="could not read old and new workflow content for classification",
        )
        return [
            _violation(
                path=path,
                protected_pattern=protected_pattern,
                section=path,
                line=None,
                reason=reason,
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
    old_jobs = _workflow_jobs(old_workflow, diff.old_text)
    new_jobs = _workflow_jobs(new_workflow, diff.new_text)
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


def _workflow_jobs(workflow: Mapping[str, Any], text: str) -> Mapping[str, object] | None:
    jobs = workflow.get("jobs")
    if jobs is None:
        return {}
    if not isinstance(jobs, Mapping):
        return None
    normalized_jobs: dict[str, object] = {}
    for key, value in jobs.items():
        job_id = _workflow_job_id(key, text)
        if job_id in normalized_jobs:
            return None
        normalized_jobs[job_id] = value
    return normalized_jobs


def _workflow_job_id(key: object, text: str) -> str:
    if isinstance(key, str):
        return key
    source_name = _workflow_source_scalar_key_name(key, _workflow_job_key_source_names(text))
    if source_name is not None:
        return source_name
    return str(key)


def _workflow_source_scalar_key_name(
    loaded_key: object,
    source_names: Sequence[str],
) -> str | None:
    matches = [
        source_name
        for source_name in source_names
        if _yaml_scalar_key_matches_loaded_key(source_name, loaded_key)
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def _workflow_job_key_source_names(text: str) -> tuple[str, ...]:
    jobs_node = _workflow_jobs_mapping_node(text)
    if jobs_node is None:
        return ()
    return tuple(
        key_node.value
        for key_node, _value_node in jobs_node.value
        if isinstance(key_node, ScalarNode)
    )


def _workflow_jobs_mapping_node(text: str) -> MappingNode | None:
    document = _compose_workflow_yaml_document(text)
    if not isinstance(document, MappingNode):
        return None
    for key_node, value_node in document.value:
        if isinstance(key_node, ScalarNode) and key_node.value == "jobs":
            if isinstance(value_node, MappingNode):
                return value_node
            return None
    return None


def _yaml_scalar_key_matches_loaded_key(source_name: str, loaded_key: object) -> bool:
    try:
        loaded = yaml.safe_load(f"{source_name}: null\n")
    except yaml.YAMLError:
        return False
    if not isinstance(loaded, Mapping) or len(loaded) != 1:
        return False
    source_key = next(iter(loaded))
    return type(source_key) is type(loaded_key) and source_key == loaded_key


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
    last_ordered_match_index = -1
    reported_order_violation = False
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
        matched_step = new_steps[match_index]
        if match_index < last_ordered_match_index and not reported_order_violation:
            label = _step_label(matched_step)
            violations.append(
                _violation(
                    path=path,
                    protected_pattern=protected_pattern,
                    section=f"jobs.{job_id}.steps.{label}",
                    line=_line_for_workflow_step(new_text, matched_step),
                    reason=f"workflow step order changed: jobs.{job_id}.steps.{label}",
                )
            )
            reported_order_violation = True
        else:
            last_ordered_match_index = match_index
        violations.extend(
            _workflow_existing_step_violations(
                path=path,
                protected_pattern=protected_pattern,
                job_id=job_id,
                old_step=old_step,
                new_step=matched_step,
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
        if not _allows_comment_continue_on_error(new_step):
            violations.append(
                _violation(
                    path=path,
                    protected_pattern=protected_pattern,
                    section=f"{section_prefix}.continue-on-error",
                    line=_line_for_workflow_step_key(new_text, new_step, key="continue-on-error"),
                    reason=(
                        "continue-on-error is only allowed for comment/notify steps "
                        "with safe command/action semantics"
                    ),
                )
            )
    elif (
        old_continue != new_continue
        and not (_is_true(old_continue) and not _is_true(new_continue))
        and not (
            _is_default_false_continue_on_error(old_continue)
            and _is_default_false_continue_on_error(new_continue)
        )
    ):
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
    is_allowed_pinned_bump = False
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

    if (
        is_allowed_pinned_bump
        and old_step.get("with") != new_step.get("with")
        and not _workflow_pinned_bump_with_inputs_are_safe(
            new_uses=new_uses,
            old_inputs=old_step.get("with"),
            new_inputs=new_step.get("with"),
        )
    ):
        violations.append(
            _violation(
                path=path,
                protected_pattern=protected_pattern,
                section=f"{section_prefix}.with",
                line=_line_for_workflow_step_key(new_text, new_step, key="with"),
                reason=(
                    "workflow action with inputs changed during pinned ref bump "
                    "with unapproved input changes, unsafe input names, or unsafe "
                    f"expressions: {section_prefix}.with"
                ),
            )
        )

    old_run = _string_value(old_step.get("run"))
    new_run = _string_value(new_step.get("run"))
    if old_run != new_run:
        old_run_is_validation = _is_validation_command(old_run)
        new_run_is_validation = _is_validation_command(new_run)
        if not old_run_is_validation and new_run_is_validation:
            violations.append(
                _violation(
                    path=path,
                    protected_pattern=protected_pattern,
                    section=f"{section_prefix}.run",
                    line=_line_for_workflow_step_key(new_text, new_step, key="run"),
                    reason="workflow validation command introduced; introducing validation command is blocked",
                )
            )
        elif old_run_is_validation:
            if not _preserves_existing_validation_run(old_run, new_run):
                violations.append(
                    _violation(
                        path=path,
                        protected_pattern=protected_pattern,
                        section=f"{section_prefix}.run",
                        line=_line_for_workflow_step_key(new_text, new_step, key="run"),
                        reason=(
                            "workflow validation command changed without preserving existing "
                            f"command: {section_prefix}.run"
                        ),
                    )
                )
        elif not _is_informational_step(new_step):
            violations.append(
                _violation(
                    path=path,
                    protected_pattern=protected_pattern,
                    section=f"{section_prefix}.run",
                    line=_line_for_workflow_step_key(new_text, new_step, key="run"),
                    reason=f"workflow run command changed outside informational step: {section_prefix}.run",
                )
            )

    if _step_remainder(old_step, ignore_with=is_allowed_pinned_bump) != _step_remainder(
        new_step, ignore_with=is_allowed_pinned_bump
    ):
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
        return ("uses", action.lower() if action is not None else uses)
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


def _allows_comment_continue_on_error(step: Mapping[str, Any]) -> bool:
    return _is_comment_or_notify_step(step) and _is_informational_step(step)


def _is_informational_job(job_id: str, job: Mapping[str, Any]) -> bool:
    if any(key not in _INFORMATIONAL_JOB_ALLOWED_KEYS for key in job):
        return False
    if not _informational_job_permissions_are_safe(job.get("permissions")):
        return False
    job_env_names = _safe_informational_env_names(job.get("env"))
    if job_env_names is None:
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
    return bool(steps) and all(
        _is_informational_step(step, inherited_env_names=job_env_names) for step in steps
    )


def _informational_job_permissions_are_safe(permissions: object) -> bool:
    if permissions is None:
        return True
    if isinstance(permissions, str):
        return permissions.strip().lower() == "read-all"
    if not isinstance(permissions, Mapping):
        return False

    for scope, level in permissions.items():
        if not isinstance(scope, str) or not isinstance(level, str):
            return False
        normalized_scope = scope.lower()
        normalized_level = level.lower()
        if normalized_scope in _INFORMATIONAL_JOB_COMMENT_PERMISSION_SCOPES:
            if normalized_level not in {"none", "read", "write"}:
                return False
        elif normalized_scope in _INFORMATIONAL_JOB_READ_PERMISSION_SCOPES:
            if normalized_level not in {"none", "read"}:
                return False
        else:
            return False
    return True


def _is_informational_step(
    step: Mapping[str, Any],
    *,
    inherited_env_names: set[str] | None = None,
) -> bool:
    if any(key not in _INFORMATIONAL_STEP_ALLOWED_KEYS for key in step):
        return False
    safe_env_names = set(inherited_env_names or ())
    step_env_names = _safe_informational_env_names(step.get("env"))
    if step_env_names is None:
        return False
    safe_env_names.update(step_env_names)
    run = _string_value(step.get("run"))
    uses = _string_value(step.get("uses"))
    if (run is None) == (uses is None):
        return False
    if uses is not None:
        if not _is_comment_or_notify_step(step):
            return False
        if not _is_comment_or_notify_capable_step_uses(step, uses):
            return False
    elif not _is_comment_or_notify_step(step):
        label = _step_label(step).lower()
        if not any(marker in label for marker in _INFORMATIONAL_MARKERS):
            return False
    return _is_informational_run_command(run, safe_env_names) and not _is_validation_command(run)


def _safe_informational_env_names(env: object) -> set[str] | None:
    if env is None:
        return set()
    if not isinstance(env, Mapping):
        return None
    safe_names: set[str] = set()
    for name, value in env.items():
        if not isinstance(name, str):
            return None
        normalized_name = name.upper()
        if (
            _ENV_NAME_RE.fullmatch(name) is None
            or _SENSITIVE_ENV_NAME_RE.search(name) is not None
            or normalized_name in _BLOCKED_INFORMATIONAL_ENV_NAMES
        ):
            return None
        if not _is_safe_informational_env_value(value):
            return None
        safe_names.add(name)
    return safe_names


def _is_safe_informational_env_value(value: object) -> bool:
    if not isinstance(value, str | int | float | bool):
        return False
    return not _has_unsafe_informational_parameter_expansion((str(value),))


def _line_for_yaml_top_level_key(text: str, key: str) -> int | None:
    return _line_matching(text, rf"^{re.escape(key)}\s*:")


def _line_for_workflow_job(text: str, job_id: str) -> int | None:
    return _line_matching(text, rf"^\s{{2,}}{re.escape(job_id)}\s*:")


def _line_for_workflow_step(text: str, step: Mapping[str, Any]) -> int | None:
    for key in ("name", "id", "uses", "run"):
        value = _string_value(step.get(key))
        if value:
            line = _line_for_workflow_step_key_from_yaml_nodes(text, step, key=key)
            if line is not None:
                return line
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
    yaml_node_line = _line_for_workflow_step_key_from_yaml_nodes(text, step, key=key)
    if yaml_node_line is not None:
        return yaml_node_line
    step_line = _line_for_workflow_step(text, step)
    lines = text.splitlines()
    if step_line is not None:
        key_pattern = re.compile(rf"^\s*(?:-\s*)?{re.escape(key)}\s*:")
        step_index = step_line - 1
        step_indent = None
        anchor_indent = len(lines[step_index]) - len(lines[step_index].lstrip())
        for candidate in range(step_index, -1, -1):
            marker = re.match(r"^(\s*)-\s+", lines[candidate])
            if marker is None:
                continue
            marker_indent = len(marker.group(1))
            if candidate == step_index or marker_indent < anchor_indent:
                step_index = candidate
                step_indent = marker_indent
                break
        if step_indent is not None:
            end_index = len(lines)
            for candidate in range(step_index + 1, len(lines)):
                marker = re.match(r"^(\s*)-\s+", lines[candidate])
                if marker is not None and len(marker.group(1)) <= step_indent:
                    end_index = candidate
                    break
            for candidate in range(step_index, end_index):
                if key_pattern.match(lines[candidate]):
                    return candidate + 1
            return None
        for index in range(step_line, min(len(lines), step_line + 12) + 1):
            if key_pattern.match(lines[index - 1]):
                return index
    return _line_matching(text, rf"^\s*{re.escape(key)}\s*:")


def _line_for_workflow_step_key_from_yaml_nodes(
    text: str,
    step: Mapping[str, Any],
    *,
    key: str,
) -> int | None:
    document = _compose_workflow_yaml_document(text)
    if document is None:
        return None
    for node in _workflow_sequence_mapping_nodes(document):
        if not _workflow_step_node_matches(node, step):
            continue
        for key_node, _value_node in node.value:
            if isinstance(key_node, ScalarNode) and key_node.value == key:
                return key_node.start_mark.line + 1
    return None


@lru_cache(maxsize=64)
def _compose_workflow_yaml_document(text: str) -> Node | None:
    try:
        return cast(Node | None, yaml.compose(text))
    except yaml.YAMLError:
        return None


def _workflow_sequence_mapping_nodes(node: Node) -> Iterator[MappingNode]:
    if isinstance(node, SequenceNode):
        for item in node.value:
            if isinstance(item, MappingNode):
                yield item
            yield from _workflow_sequence_mapping_nodes(item)
        return
    if isinstance(node, MappingNode):
        for _key_node, value_node in node.value:
            yield from _workflow_sequence_mapping_nodes(value_node)


def _workflow_step_node_matches(node: MappingNode, step: Mapping[str, Any]) -> bool:
    scalar_values: dict[str, str] = {}
    for key_node, value_node in node.value:
        if isinstance(key_node, ScalarNode) and isinstance(value_node, ScalarNode):
            scalar_values[key_node.value] = value_node.value
    expected_values = {
        key: step_value
        for key in ("name", "id", "uses", "run")
        if (step_value := _string_value(step.get(key)))
    }
    return bool(expected_values) and all(
        scalar_values.get(key) == step_value for key, step_value in expected_values.items()
    )


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


from awf.control.quality_gates_workflow_actions import (  # noqa: E402
    _is_default_false_continue_on_error,
    _is_pinned_uses_bump,
    _is_true,
    _line_for_yaml_key,
    _step_remainder,
    _string_value,
    _uses_action,
    _workflow_pinned_bump_with_inputs_are_safe,
)
from awf.control.quality_gates_workflow_commands import (  # noqa: E402
    _has_unsafe_informational_parameter_expansion,
    _is_comment_or_notify_capable_step_uses,
    _is_informational_run_command,
    _is_validation_command,
    _preserves_existing_validation_run,
)
