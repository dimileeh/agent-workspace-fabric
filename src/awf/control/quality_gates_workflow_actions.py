"""Quality-gate protections for agent-authored workspace changes."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
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


def _has_unsafe_github_actions_expression(tokens: Sequence[str]) -> bool:
    from awf.control.quality_gates_workflow_commands import (
        _has_unsafe_github_actions_expression as impl,
    )

    return impl(tokens)


def _comment_notify_action_with_inputs_are_safe(action: str, inputs: object) -> bool:
    if inputs is None:
        return True
    if not isinstance(inputs, Mapping):
        return False
    allowed_keys = _COMMENT_NOTIFY_ACTION_ALLOWED_WITH_KEYS.get(action, frozenset())
    for key, value in inputs.items():
        if not isinstance(key, str):
            return False
        if key.lower() not in allowed_keys:
            return False
        if not _comment_notify_action_with_value_is_safe(value):
            return False
    return True


def _github_script_comment_notify_inputs_are_safe(inputs: object) -> bool:
    if inputs is None:
        return False
    if not isinstance(inputs, Mapping):
        return False
    script: object = None
    for key, value in inputs.items():
        if not isinstance(key, str):
            return False
        normalized_key = key.lower()
        if normalized_key not in _GITHUB_SCRIPT_COMMENT_ALLOWED_WITH_KEYS:
            return False
        if normalized_key == "script":
            if not isinstance(value, str):
                return False
            if _has_unsafe_github_actions_expression((value,)):
                return False
            script = value
        elif not _comment_notify_action_with_value_is_safe(value):
            return False
    return isinstance(script, str) and _github_script_comment_notify_script_is_safe(script)


def _github_script_comment_notify_script_is_safe(script: str) -> bool:
    if _GITHUB_SCRIPT_BLOCKED_ACCESS_RE.search(script) is not None:
        return False
    rest_methods = tuple(_GITHUB_SCRIPT_REST_METHOD_RE.findall(script))
    if not rest_methods:
        return False
    return all(method in _GITHUB_SCRIPT_COMMENT_ALLOWED_REST_METHODS for method in rest_methods)


def _comment_notify_action_with_value_is_safe(value: object) -> bool:
    if isinstance(value, str):
        return not _has_unsafe_github_actions_expression((value,))
    if value is None:
        return True
    return isinstance(value, bool | int | float)


def _workflow_pinned_bump_with_inputs_are_safe(
    *,
    new_uses: str | None,
    old_inputs: object,
    new_inputs: object,
) -> bool:
    if new_uses is None:
        return False
    old_map = _normalized_workflow_with_inputs(old_inputs)
    new_map = _normalized_workflow_with_inputs(new_inputs)
    if old_map is None or new_map is None:
        return False
    changed_keys = {
        key for key in set(old_map) | set(new_map) if old_map.get(key) != new_map.get(key)
    }
    if not changed_keys:
        return True
    allowed_keys = _workflow_pinned_bump_allowed_with_keys(new_uses)
    if not changed_keys <= allowed_keys:
        return False
    if any(key not in old_map or key not in new_map for key in changed_keys):
        return False
    changed_inputs = {key: new_map[key] for key in changed_keys}
    return _workflow_with_inputs_have_safe_names_and_values(changed_inputs)


def _workflow_pinned_bump_allowed_with_keys(uses: str) -> frozenset[str]:
    parts = _uses_action_and_ref(uses)
    if parts is None:
        return frozenset()
    action, _ref = parts
    return _WORKFLOW_PINNED_BUMP_ALLOWED_WITH_KEYS.get(action.lower(), frozenset())


def _normalized_workflow_with_inputs(inputs: object) -> dict[str, object] | None:
    if inputs is None:
        return {}
    if not isinstance(inputs, Mapping):
        return None
    normalized: dict[str, object] = {}
    for key, value in inputs.items():
        if not isinstance(key, str):
            return None
        normalized_key = _normalize_workflow_with_input_key(key)
        if not normalized_key or normalized_key in normalized:
            return None
        normalized[normalized_key] = value
    return normalized


def _workflow_with_inputs_have_safe_names_and_values(inputs: object) -> bool:
    if inputs is None:
        return True
    if not isinstance(inputs, Mapping):
        return False
    for key, value in inputs.items():
        if not isinstance(key, str):
            return False
        if _is_sensitive_workflow_with_input_key(key):
            return False
        if not _workflow_with_input_value_is_safe(value):
            return False
    return True


def _normalize_workflow_with_input_key(key: str) -> str:
    return key.strip().lower().replace("_", "-")


def _is_sensitive_workflow_with_input_key(key: str) -> bool:
    normalized = _normalize_workflow_with_input_key(key)
    parts = tuple(part for part in normalized.split("-") if part)
    if any(part in _SENSITIVE_WORKFLOW_WITH_INPUT_PARTS for part in parts):
        return True
    return any(name in normalized for name in _SENSITIVE_WORKFLOW_WITH_INPUT_NAMES)


def _workflow_with_input_value_is_safe(value: object) -> bool:
    if isinstance(value, str):
        return not _has_unsafe_github_actions_expression((value,))
    if value is None:
        return True
    return isinstance(value, bool | int | float)


def _is_pinned_uses_bump(old_uses: str, new_uses: str) -> bool:
    old_parts = _uses_action_and_ref(old_uses)
    new_parts = _uses_action_and_ref(new_uses)
    if old_parts is None or new_parts is None:
        return False
    old_action, old_ref = old_parts
    new_action, new_ref = new_parts
    if old_action.lower() != new_action.lower() or old_ref == new_ref:
        return False
    if not _is_pinned_workflow_uses_ref(old_ref) or not _is_pinned_workflow_uses_ref(new_ref):
        return False
    old_is_sha = _PINNED_WORKFLOW_USES_SHA_RE.fullmatch(old_ref) is not None
    new_is_sha = _PINNED_WORKFLOW_USES_SHA_RE.fullmatch(new_ref) is not None
    if new_is_sha:
        # Arbitrary SHAs cannot be ordered locally against version tags or other
        # SHAs, so unowned protected workflow edits must fail closed here.
        return False
    if old_is_sha:
        # A raw SHA cannot be ordered against a tag without resolving refs; locally
        # we can only require the replacement to be a fully pinned version tag.
        return _is_full_workflow_version_ref(new_ref)
    return _is_workflow_version_ref_non_downgrade(old_ref, new_ref)


def _is_workflow_version_ref_non_downgrade(old_ref: str, new_ref: str) -> bool:
    old_key = _workflow_version_ref_sort_key(old_ref)
    new_key = _workflow_version_ref_sort_key(new_ref)
    if old_key is None or new_key is None:
        return False
    if new_key < old_key:
        return False
    if _has_same_core_simple_prerelease_numeric_suffix_downgrade(
        old_ref,
        new_ref,
        old_key,
        new_key,
    ):
        return False
    return not _has_same_core_mixed_prerelease_label_change(old_ref, new_ref, old_key, new_key)


def _is_full_workflow_version_ref(ref: str) -> bool:
    if _PINNED_WORKFLOW_USES_VERSION_RE.fullmatch(ref) is None:
        return False
    raw_version = ref[1:] if ref.startswith(("v", "V")) else ref
    core = raw_version.split("+", 1)[0].split("-", 1)[0]
    return len(core.split(".")) >= 3


def _workflow_version_ref_sort_key(ref: str) -> _WorkflowVersionRefSortKey | None:
    if _PINNED_WORKFLOW_USES_VERSION_RE.fullmatch(ref) is None:
        return None
    raw_version = ref[1:] if ref.startswith(("v", "V")) else ref
    version_without_build = raw_version.split("+", 1)[0]
    core, separator, prerelease = version_without_build.partition("-")
    numbers = [int(part) for part in core.split(".")]
    while len(numbers) < 3:
        numbers.append(0)
    release_rank = 0 if separator else 1
    return tuple(numbers[:3]), release_rank, _workflow_prerelease_sort_key(prerelease)


def _workflow_prerelease_sort_key(
    prerelease: str,
) -> tuple[_WorkflowPrereleaseIdentifierKey, ...]:
    if not prerelease:
        return ()
    return tuple(
        _workflow_prerelease_identifier_sort_key(identifier) for identifier in prerelease.split(".")
    )


def _workflow_prerelease_identifier_sort_key(
    identifier: str,
) -> _WorkflowPrereleaseIdentifierKey:
    if identifier.isdigit():
        return 0, int(identifier)
    return 1, identifier


def _has_same_core_mixed_prerelease_label_change(
    old_ref: str,
    new_ref: str,
    old_key: _WorkflowVersionRefSortKey,
    new_key: _WorkflowVersionRefSortKey,
) -> bool:
    if old_key[0] != new_key[0]:
        return False
    old_prerelease = _workflow_version_ref_prerelease(old_ref)
    new_prerelease = _workflow_version_ref_prerelease(new_ref)
    if not old_prerelease or not new_prerelease or old_prerelease == new_prerelease:
        return False
    return _has_mixed_prerelease_identifier(old_prerelease) or _has_mixed_prerelease_identifier(
        new_prerelease
    )


def _has_same_core_simple_prerelease_numeric_suffix_downgrade(
    old_ref: str,
    new_ref: str,
    old_key: _WorkflowVersionRefSortKey,
    new_key: _WorkflowVersionRefSortKey,
) -> bool:
    if old_key[0] != new_key[0]:
        return False
    old_identifiers = _workflow_version_ref_prerelease(old_ref).split(".")
    new_identifiers = _workflow_version_ref_prerelease(new_ref).split(".")
    if len(old_identifiers) != len(new_identifiers):
        return False
    for old_identifier, new_identifier in zip(old_identifiers, new_identifiers, strict=True):
        old_parts = _simple_prerelease_numeric_suffix_parts(old_identifier)
        new_parts = _simple_prerelease_numeric_suffix_parts(new_identifier)
        if old_parts is None or new_parts is None:
            continue
        old_label, old_number = old_parts
        new_label, new_number = new_parts
        if old_label == new_label and new_number < old_number:
            return True
    return False


def _workflow_version_ref_prerelease(ref: str) -> str:
    raw_version = ref[1:] if ref.startswith(("v", "V")) else ref
    version_without_build = raw_version.split("+", 1)[0]
    _core, separator, prerelease = version_without_build.partition("-")
    return prerelease if separator else ""


def _has_mixed_prerelease_identifier(prerelease: str) -> bool:
    return any(
        not identifier.isdigit()
        and any(character.isdigit() for character in identifier)
        and _PRERELEASE_NUMERIC_SUFFIX_RE.fullmatch(identifier) is None
        for identifier in prerelease.split(".")
    )


def _simple_prerelease_numeric_suffix_parts(identifier: str) -> tuple[str, int] | None:
    match = _PRERELEASE_NUMERIC_SUFFIX_RE.fullmatch(identifier)
    if match is None:
        return None
    return match.group(1), int(match.group(2))


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
    *,
    ignore_with: bool = False,
) -> dict[str, object]:
    ignored = {"continue-on-error", "id", "if", "name", "run", "uses"}
    if ignore_with:
        ignored = ignored | {"with"}
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


def _is_default_false_continue_on_error(value: object) -> bool:
    return value is None or value is False or (isinstance(value, str) and value.lower() == "false")


def _string_value(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _format_number(value: float) -> str:
    return str(int(value)) if value.is_integer() else f"{value:g}"


def _format_toml_policy_value(value: object) -> str:
    if value is None:
        return "unset"
    if _is_number(value):
        return _format_number(float(cast(int | float, value)))
    return repr(value)


def _line_for_yaml_key(text: str, key: str) -> int | None:
    from awf.control.quality_gates_workflow import _line_matching

    return _line_matching(text, rf"^\s*{re.escape(key)}\s*:")
